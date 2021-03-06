import torch
from torch.autograd import Function, Variable
from torch.nn import Module
from torch.nn.parameter import Parameter

import numpy as np
import numpy.random as npr

from enum import Enum

import sys

from . import util
from .pnqp import pnqp
from .lqr_step import LQRStep
from .dynamics import CtrlPassthroughDynamics

class GradMethods(Enum):
    AUTO_DIFF = 1
    FINITE_DIFF = 2
    ANALYTIC = 3
    ANALYTIC_CHECK = 4

class MPC(Module):
    """A differentiable box-constrained MPC/iLQR solver.

    This provides a differentiable solver for the following box-constrained
    control problem with a quadratic cost (defined by C and c) and
    non-linear dynamics (defined by f):

        min_{tau={x,u}} sum_t 0.5 tau_t^T C_t tau_t + c_t^T tau_t
                        s.t. x_{t+1} = f(x_t, u_t)
                            x_0 = x_init
                            u_lower <= u <= u_upper

    This implements the Control-Limited Differential Dynamic Programming
    paper with a first-order approximation to the non-linear dynamics:
    https://homes.cs.washington.edu/~todorov/papers/TassaICRA14.pdf

    Some of the notation here is from Sergey Levine's notes:
    http://rll.berkeley.edu/deeprlcourse/f17docs/lecture_8_model_based_planning.pdf

    Required Args:
        n_state, n_ctrl, T
        x_init: The initial state [n_batch, n_state]

    Optional Args:
        u_lower, u_upper: The lower- and upper-bounds on the controls.
            These can either be floats or shaped as [T, n_batch, n_ctrl]
            TODO: Better support automatic expansion of these.
        u_init: The initial control sequence, useful for warm-starting:
            [T, n_batch, n_ctrl]
        lqr_iter: The number of LQR iterations to perform.
        grad_method: The method to compute the Jacobian of the dynamics function.
            GradMethods.ANALYTIC: Use a manually-defined Jacobian.
                + Fast and accurate, use this if possible
            GradMethods.AUTO_DIFF: Use PyTorch's autograd.
                + Slow
            GradMethods.FINITE_DIFF: Use naive finite differences
                + Inaccurate
        delta_u (float): The amount each component of the controls
            is allowed to change in each LQR iteration.
        verbose (int):
            -1: No output or warnings
             0: Warnings
            1+: Detailed iteration info
        eps: Termination threshold, on the norm of the full control
             step (without line search)
        n_batch: May be necessary for now if it can't be inferred.
                 TODO: Infer, potentially remove this.
        max_linesearch_iter: Can be used to disable the line search
            if 1 is used for some problems the line search can
            be harmful.
        exit_unconverged: Assert False if a fixed point is not reached.
            TODO: Potentially remove this
        backprop: Allow the solver to be differentiated through.
            TODO: Potentially remove this
    """

    def __init__(
            self, n_state, n_ctrl, T, x_init,
            u_lower=None, u_upper=None,
            u_zero_I=None,
            u_init=None,
            lqr_iter=10,
            grad_method=GradMethods.ANALYTIC,
            delta_u=None,
            verbose=0,
            eps=1e-7,
            back_eps=1e-7,
            n_batch=None,
            linesearch_decay=0.2,
            max_linesearch_iter=10,
            exit_unconverged=True,
            detach_unconverged=True,
            backprop=True,
            F=None,
            f=None,
            slew_rate_penalty=None,
            prev_ctrl=None,
            not_improved_lim=5,
            best_cost_eps=1e-4
    ):
        super().__init__()

        assert (u_lower is None) == (u_upper is None)
        assert max_linesearch_iter > 0

        self.n_state = n_state
        self.n_ctrl = n_ctrl
        self.T = T
        self.x_init = x_init
        if not isinstance(self.x_init, Variable):
            self.x_init = Variable(self.x_init)
        self.u_lower = util.get_data_maybe(u_lower)
        self.u_upper = util.get_data_maybe(u_upper)
        self.u_zero_I = util.get_data_maybe(u_zero_I)
        self.u_init = util.get_data_maybe(u_init)
        self.lqr_iter = lqr_iter
        self.grad_method = grad_method
        self.delta_u = delta_u
        self.verbose = verbose
        self.eps = eps
        self.back_eps = back_eps
        self.n_batch = n_batch
        self.linesearch_decay = linesearch_decay
        self.max_linesearch_iter = max_linesearch_iter
        self.exit_unconverged = exit_unconverged
        self.detach_unconverged = detach_unconverged
        self.backprop = backprop
        self.not_improved_lim = not_improved_lim
        self.best_cost_eps = best_cost_eps

        # Affine dynamics coefficients.
        self.F = F
        self.f = f

        assert f is None # TODO: Should work but untested.

        self.slew_rate_penalty = slew_rate_penalty
        self.prev_ctrl = prev_ctrl

        if slew_rate_penalty is not None:
            assert F is None and f is None, 'unimplemented'


    # @profile
    def forward(self, C, c, dynamics=None):
        """Solve the control problem.

        Let n_tau = n_state+n_control

        Args:
            C: The quadratic coefficients in the objective [T, n_batch, n_tau, n_tau]
            c: The linear coefficients in the objective [T, n_batch, n_tau]
            dynamics
        """
        # TODO: Clean up inferences, expansions, and assumptions made here.
        if C.ndimension() == 4:
            n_batch = C.size(1)
        elif self.n_batch is not None:
            n_batch = self.n_batch
        else:
            assert False, 'Could not infer batch size, pass in as n_batch'

        if C.ndimension() == 3:
            C = C.unsqueeze(1).expand(self.T, n_batch, self.n_state+self.n_ctrl, -1)
        if c.ndimension() == 2:
            c = c.unsqueeze(1).expand(self.T, n_batch, -1)

        x_init = self.x_init
        assert x_init.ndimension() == 2 and x_init.size(0) == n_batch

        if self.u_init is None:
            u = torch.zeros(self.T, n_batch, self.n_ctrl).type_as(x_init.data)
        else:
            u = self.u_init
            if u.ndimension() == 2:
                u = u.unsqueeze(1).expand(self.T, n_batch, -1).clone()
        u = u.type_as(x_init.data)

        if self.verbose > 0:
            print('Initial mean(cost): {:.4e}'.format(
                torch.mean(util.get_cost(
                    self.T, C, c, u, dynamics=dynamics, x_init=x_init,
                    F=self.F, f=self.f))))

        best = None

        F = self.F
        f = self.f
        n_not_improved = 0
        for i in range(self.lqr_iter):
            u = Variable(util.get_data_maybe(u), requires_grad=True)
            # Linearize the dynamics around the current trajectory.
            if dynamics is not None:
                x = util.get_traj(self.T, u, x_init=x_init, dynamics=dynamics)
                x = torch.stack(x, 0)
                F, f = self.linearize_dynamics(
                    x, util.get_data_maybe(u), dynamics, diff=False)
            else:
                x = util.get_traj(
                    self.T, u, x_init=x_init, F=F, f=f,
                )

            x, u, _lqr = self.solve_lqr_subproblem(
                x_init, C, c, F, f, dynamics, x, u)
            back_out = _lqr.back_out
            for_out = _lqr.for_out
            n_not_improved += 1

            assert x.ndimension() == 3
            assert u.ndimension() == 3

            if best is None:
                best = {
                    'x': list(torch.split(x, split_size=1, dim=1)),
                    'u': list(torch.split(u, split_size=1, dim=1)),
                    'costs': for_out.costs,
                    'full_du_norm': for_out.full_du_norm,
                }
            else:
                for j in range(n_batch):
                    if for_out.costs[j] <= best['costs'][j] - self.best_cost_eps:
                        n_not_improved = 0
                        best['x'][j] = x[:,j].unsqueeze(1)
                        best['u'][j] = u[:,j].unsqueeze(1)
                        best['costs'][j] = for_out.costs[j]
                        best['full_du_norm'][j] = for_out.full_du_norm[j]

            if self.verbose > 0:
                util.table_log('lqr', (
                    ('iter', i),
                    ('mean(cost)', torch.mean(best['costs']), '{:.4e}'),
                    ('||full_du||_max', max(for_out.full_du_norm), '{:.2e}'),
                    # ('||alpha_du||_max', max(for_out.alpha_du_norm), '{:.2e}'),
                    # TODO: alphas, total_qp_iters here is for the current
                    # iterate, not the best
                    ('mean(alphas)', for_out.mean_alphas, '{:.2e}'),
                    ('total_qp_iters', back_out.n_total_qp_iter),
                ))

            if max(for_out.full_du_norm) < self.eps or \
               n_not_improved > self.not_improved_lim:
                break

        x = torch.cat(best['x'], dim=1).data
        u = torch.cat(best['u'], dim=1).data
        full_du_norm = best['full_du_norm']

        if dynamics is not None:
            F, f = self.linearize_dynamics(x, u, dynamics, diff=True)

        x, u, self._lqr_step = self.solve_lqr_subproblem(
            x_init, C, c, F, f, dynamics, x, u, no_op_forward=True)

        if self.detach_unconverged:
            if max(best['full_du_norm']) > self.eps:
                if self.exit_unconverged:
                    assert False

                if self.verbose >= 0:
                    print("LQR Warning: All examples did not converge to a fixed point.")
                    print("Detaching and *not* backpropping through the bad examples.")

                I = for_out.full_du_norm < self.eps
                Ix = Variable(I.unsqueeze(0).unsqueeze(2).expand_as(x)).type_as(x.data)
                Iu = Variable(I.unsqueeze(0).unsqueeze(2).expand_as(u)).type_as(u.data)
                x = x*Ix + x.clone().detach()*(1.-Ix)
                u = u*Iu + u.clone().detach()*(1.-Iu)

        costs = best['costs']
        return (x, u, costs)

    def solve_lqr_subproblem(self, x_init, C, c, F, f, dynamics, x, u,
                             no_op_forward=False):
        if self.slew_rate_penalty is None:
            _lqr = LQRStep(
                n_state=self.n_state,
                n_ctrl=self.n_ctrl,
                T=self.T,
                u_lower=self.u_lower,
                u_upper=self.u_upper,
                u_zero_I=self.u_zero_I,
                true_dynamics=dynamics,
                delta_u=self.delta_u,
                linesearch_decay=self.linesearch_decay,
                max_linesearch_iter=self.max_linesearch_iter,
                delta_space=True,
                current_x=x,
                current_u=u,
                back_eps=self.back_eps,
                no_op_forward=no_op_forward,
            )
            e = Variable(torch.Tensor())
            x, u = _lqr(x_init, C, c, F, f if f is not None else e)

            return x, u, _lqr
        else:
            nsc = self.n_state + self.n_ctrl
            _n_state = nsc
            _nsc = _n_state + self.n_ctrl
            n_batch = C.size(1)
            _C = torch.zeros(self.T, n_batch, _nsc, _nsc).type_as(C.data)
            half_gamI = self.slew_rate_penalty*torch.eye(
                self.n_ctrl).unsqueeze(0).unsqueeze(0).repeat(self.T, n_batch, 1, 1)
            _C[:,:,:self.n_ctrl,:self.n_ctrl] = half_gamI
            _C[:,:,-self.n_ctrl:,:self.n_ctrl] = -half_gamI
            _C[:,:,:self.n_ctrl,-self.n_ctrl:] = -half_gamI
            _C[:,:,-self.n_ctrl:,-self.n_ctrl:] = half_gamI
            _C = Variable(_C)
            _C += torch.nn.ZeroPad2d((self.n_ctrl, 0, self.n_ctrl, 0))(C)

            _c = torch.cat((
                Variable(torch.zeros(self.T, n_batch, self.n_ctrl).type_as(c.data)),
                c
            ), 2)

            _F0 = Variable(torch.cat((
                torch.zeros(self.n_ctrl, self.n_state+self.n_ctrl),
                torch.eye(self.n_ctrl),
            ), 1).type_as(F.data).unsqueeze(0).unsqueeze(0).repeat(
                self.T-1, n_batch, 1, 1
            ))
            _F1 = torch.cat((
                Variable(torch.zeros(
                    self.T-1, n_batch, self.n_state, self.n_ctrl
                ).type_as(F.data)),
                F
            ), 3)
            _F = torch.cat((_F0, _F1), 2)

            if f is not None:
                _f = torch.cat((
                    Variable(torch.zeros(
                        self.T-1, n_batch, self.n_ctrl
                    ).type_as(f.data)),
                    f
                ), 2)
            else:
                _f = Variable(torch.Tensor())

            u_data = util.get_data_maybe(u)
            if self.prev_ctrl is not None:
                prev_u = self.prev_ctrl
                if prev_u.ndimension() == 1:
                    prev_u = prev_u.unsqueeze(0)
                if prev_u.ndimension() == 2:
                    prev_u = prev_u.unsqueeze(0)
                prev_u = util.get_data_maybe(prev_u)
            else:
                prev_u = torch.zeros(1, n_batch, self.n_ctrl).type_as(u_data)
            utm1s = torch.cat((prev_u, u_data[:-1])).clone()
            _x = torch.cat((utm1s, x), 2)

            _x_init = torch.cat((Variable(prev_u[0]), x_init), 1)

            if dynamics is not None:
                _dynamics = CtrlPassthroughDynamics(dynamics)

            _lqr = LQRStep(
                n_state=_n_state,
                n_ctrl=self.n_ctrl,
                T=self.T,
                u_lower=self.u_lower,
                u_upper=self.u_upper,
                u_zero_I=self.u_zero_I,
                true_dynamics=_dynamics,
                delta_u=self.delta_u,
                linesearch_decay=self.linesearch_decay,
                max_linesearch_iter=self.max_linesearch_iter,
                delta_space=True,
                current_x=_x,
                current_u=u,
                back_eps=self.back_eps,
                no_op_forward=no_op_forward,
            )
            x, u = _lqr(_x_init, _C, _c, _F, _f)
            x = x[:,:,self.n_ctrl:]

            return x, u, _lqr


    # @profile
    def linearize_dynamics(self, x, u, dynamics, diff):
        # TODO: Cleanup variable usage.
        assert not isinstance(x, Variable)
        assert not isinstance(u, Variable)
        # assert u.requires_grad

        n_batch = x[0].size(0)

        if self.grad_method == GradMethods.ANALYTIC:
            _u = Variable(u[:-1].view(-1, self.n_ctrl), requires_grad=True)
            _x = Variable(x[:-1].contiguous().view(-1, self.n_state),
                          requires_grad=True)

            # This inefficiently calls dynamics again, but is worth it because
            # we can efficiently compute grad_input for every time step at once.
            _new_x = dynamics(_x, _u)

            # This check is a little expensive and should only be done if
            # modifying this code.
            # assert torch.abs(_new_x.data - torch.cat(x[1:])).max() <= 1e-6

            if not diff:
                _new_x = _new_x.data
                _x = _x.data
                _u = _u.data

            R, S = dynamics.grad_input(_x, _u)

            f = _new_x - util.bmv(R, _x) - util.bmv(S, _u)
            f = f.view(self.T-1, n_batch, self.n_state)

            R = R.contiguous().view(self.T-1, n_batch, self.n_state, self.n_state)
            S = S.contiguous().view(self.T-1, n_batch, self.n_state, self.n_ctrl)
            F = torch.cat((R, S), 3)

            if not diff:
                F, f = list(map(Variable, [F, f]))
            return F, f
        else:
            # TODO: This is inefficient and confusing.
            x_init = x[0]
            x = [x_init]
            F, f = [], []
            for t in range(self.T):
                if t < self.T-1:
                    xt = Variable(x[t], requires_grad=True)
                    ut = Variable(u[t], requires_grad=True)
                    xut = torch.cat((xt, ut), 1)
                    new_x = dynamics(xt, ut)

                    # Linear dynamics approximation.
                    if self.grad_method in [GradMethods.AUTO_DIFF,
                                             GradMethods.ANALYTIC_CHECK]:
                        Rt, St = [], []
                        for j in range(self.n_state):
                            # TODO: This probably leaks memory.
                            # Check retain/create_graph here
                            # retain_graph = i < n_state-1
                            Rj, Sj = torch.autograd.grad(
                                new_x[:,j].sum(), [xt, ut],
                                retain_graph=True, create_graph=True)
                            if not diff:
                                Rj, Sj = Rj.data, Sj.data
                            Rt.append(Rj)
                            St.append(Sj)
                        Rt = torch.stack(Rt, dim=1)
                        St = torch.stack(St, dim=1)

                        if self.grad_method == GradMethods.ANALYTIC_CHECK:
                            assert False # Not updated
                            Rt_autograd, St_autograd = Rt, St
                            Rt, St = dynamics.grad_input(xt, ut)
                            eps = 1e-8
                            if torch.max(torch.abs(Rt-Rt_autograd)).data[0] > eps or \
                            torch.max(torch.abs(St-St_autograd)).data[0] > eps:
                                print('''
        nmpc.ANALYTIC_CHECK error: The analytic derivative of the dynamics function may be off.
                                ''')
                            else:
                                print('''
        nmpc.ANALYTIC_CHECK: The analytic derivative of the dynamics function seems correct.
        Re-run with GradMethods.ANALYTIC to continue.
                                ''')
                            sys.exit(0)
                    elif self.grad_method == GradMethods.FINITE_DIFF:
                        Rt, St = [], []
                        for i in range(n_batch):
                            Ri = util.jacobian(
                                lambda s: dynamics(s, ut[i]), xt[i], 1e-4
                            )
                            Si = util.jacobian(
                                lambda a : dynamics(xt[i], a), ut[i], 1e-4
                            )
                            if not diff:
                                Ri, Si = Ri.data, Si.data
                            Rt.append(Ri)
                            St.append(Si)
                        Rt = torch.stack(Rt)
                        St = torch.stack(St)
                    else:
                        assert False

                    Ft = torch.cat((Rt, St), 2)
                    F.append(Ft)

                    if not diff:
                        xt, ut, new_x = xt.data, ut.data, new_x.data
                    ft = new_x - util.bmv(Rt, xt) - util.bmv(St, ut)
                    f.append(ft)

                if t < self.T-1:
                    x.append(util.get_data_maybe(new_x))

            F = torch.stack(F, 0)
            f = torch.stack(f, 0)
            if not diff:
                F, f = list(map(Variable, [F, f]))
            return F, f
