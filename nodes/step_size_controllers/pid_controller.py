from typing import Any, Callable, Dict, Optional, Tuple

import torch
import torch.nn as nn
from torchode import status_codes
from torchode.problems import InitialValueProblem
from torchode.single_step_methods import StepResult
from torchode.step_size_controllers import PIDState, rms_norm
from torchode.terms import ODETerm
from torchode.typing import *


class PIDController(nn.Module):
    """A PID step size controller.

    The formula for the dt scaling factor with PID control is taken from [1], Equation
    (34).

    References
    ----------
    [1] Söderlind, G. (2003). Digital Filters in Adaptive Time-Stepping. ACM
        Transactions on Mathematical Software, 29, 1–26.
    """

    def __init__(
        self,
        atol: float,
        rtol: float,
        pcoeff: float,
        icoeff: float,
        dcoeff: float,
        *,
        term: Optional[ODETerm] = None,
        norm: Callable[[DataTensor], NormTensor] = rms_norm,
        force_monotonic_solve: Optional[bool] = True,
        dt_min: Optional[float] = None,
        dt_max: Optional[float] = None,
        safety: float = 0.9,
        factor_min: float = 0.2,
        factor_max: float = 10.0,
    ):
        super().__init__()

        self.register_buffer("atol", torch.tensor(atol))
        self.register_buffer("rtol", torch.tensor(rtol))
        self.term = term
        self.norm = norm
        self.force_monotonic_solve = force_monotonic_solve
        self.dt_min = dt_min
        self.dt_max = dt_max

        self.pcoeff = pcoeff
        self.icoeff = icoeff
        self.dcoeff = dcoeff
        self.safety = safety
        self.factor_min = factor_min
        self.factor_max = factor_max

    def dt_factor(self, state: PIDState, error_ratio: NormTensor):
        """Compute the growth factor of the timestep."""

        # This is an instantiation of Equation (34) in the Söderlind paper where we have
        # factored out the safety coefficient. I have not found a reference for dividing
        # the PID coefficients by the order of the solver but DifferentialEquations.jl
        # and diffrax both do it, so we do it too. Note that our error ratio is the
        # reciprocal of Söderlind's error ratio (except for the safety factor).
        # Therefore, the factor exponents have the opposite sign from the paper.
        #
        # Interesting thing from the introduction of that paper is that you work with p
        # if you want per-step-error-control and p+1 if you want
        # per-unit-step-error-control where p is the convergence order of the stepping
        # method.
        order = state.method_order
        k_I, k_P, k_D = self.icoeff / order, self.pcoeff / order, self.dcoeff / order

        factor1 = error_ratio ** (-(k_I + k_P + k_D))
        factor2 = state.prev_error_ratio ** (k_P + 2 * k_D)
        factor3 = state.prev_prev_error_ratio**-k_D
        factor = self.safety * factor1 * factor2 * factor3

        return torch.clamp(factor, min=self.factor_min, max=self.factor_max)

    def initial_state(
        self,
        method_order: int,
        problem: InitialValueProblem,
        dt_min: Optional[TimeTensor],
        dt_max: Optional[TimeTensor],
    ) -> PIDState:
        return PIDState.default(
            method_order=method_order,
            batch_size=problem.batch_size,
            dtype=problem.data_dtype,
            device=problem.device,
            dt_min=dt_min,
            dt_max=dt_max,
        )

    @torch.jit.export
    def merge_states(self, running: AcceptTensor, current: PIDState, previous: PIDState) -> PIDState:
        return current.update_error_ratios(
            torch.where(running, current.prev_error_ratio, previous.prev_error_ratio),
            torch.where(running, current.prev_prev_error_ratio, previous.prev_prev_error_ratio),
        )

    def update_state(
        self,
        state: PIDState,
        y0: DataTensor,
        dt: TimeTensor,
        error_ratio: Optional[NormTensor],
        accept: Optional[AcceptTensor],
    ) -> PIDState:
        if error_ratio is None:
            return state.update_error_ratios(
                prev_error_ratio=y0.new_ones(dt.shape),
                prev_prev_error_ratio=state.prev_error_ratio,
            )
        else:
            assert accept is not None
            return state.update_error_ratios(
                prev_error_ratio=torch.where(accept, error_ratio, state.prev_error_ratio),
                prev_prev_error_ratio=torch.where(accept, state.prev_error_ratio, state.prev_prev_error_ratio),
            )

    ################################################################################
    # The following methods should be on AdaptiveStepSizeController if TorchScript #
    # supports inheritance at some point                                           #
    ################################################################################

    @torch.jit.export
    def init(
        self,
        term: Optional[ODETerm],
        problem: InitialValueProblem,
        method_order: int,
        dt0: Optional[TimeTensor],
        *,
        stats: Dict[str, Any],
        args: Any,
    ) -> Tuple[TimeTensor, PIDState, Optional[DataTensor]]:
        if dt0 is None:
            dt_max = (problem.t_end - problem.t_start).abs()
            dt0, f0 = self._select_initial_step(
                term,
                problem.t_start,
                problem.y0,
                problem.time_direction,
                dt_max,
                method_order,
                stats,
                args,
            )
        else:
            f0 = None
        dt_min = self.dt_min
        if dt_min is not None:
            dt_min = torch.tensor(dt_min, dtype=problem.time_dtype, device=problem.device)
        dt_max = self.dt_max
        if dt_max is not None:
            dt_max = torch.tensor(dt_max, dtype=problem.time_dtype, device=problem.device)
        return dt0, self.initial_state(method_order, problem, dt_min, dt_max), f0

    @torch.jit.export
    def adapt_step_size(
        self,
        t0: TimeTensor,
        dt: TimeTensor,
        y0: DataTensor,
        step_result: StepResult,
        state: PIDState,
        stats: Dict[str, Any],
    ) -> Tuple[AcceptTensor, TimeTensor, PIDState, Optional[StatusTensor]]:
        y1, error_estimate = step_result.y, step_result.error_estimate

        if error_estimate is None:
            # If the stepping method could not provide an error estimate, we interpret
            # this as an error estimate that gets the step accepted without changing the
            # step size, i.e. as an error ratio of 1 (disregarding the safety factor).
            return (
                torch.ones_like(dt, dtype=torch.bool),
                dt,
                self.update_state(state, y0, dt, None, None),
                None,
            )

        # Compute error ratio and decide on step acceptance
        error_bounds = torch.add(self.atol, torch.maximum(y0.abs(), y1.abs()), alpha=self.rtol)
        error = error_estimate.abs()
        # We lower-bound the error ratio by some small number to avoid division by 0 in
        # `dt_factor`.
        error_ratio = torch.maximum(self.norm(error / error_bounds), state.almost_zero)
        accept = error_ratio < 1.0

        # Adapt the step size
        dt_next = dt * self.dt_factor(state, error_ratio).to(dtype=dt.dtype)

        # Check for infinities and NaN
        status = torch.where(
            torch.isfinite(error_ratio),
            status_codes.SUCCESS,
            status_codes.INFINITE_NORM,
        )

        # Enforce the minimum and maximum step size
        dt_min = state.dt_min
        dt_max = state.dt_max
        if dt_min is not None or dt_max is not None:
            dt_next = torch.clamp(dt_next, dt_min, dt_max)

        return (
            accept,
            dt_next,
            self.update_state(state, y0, dt, error_ratio, accept),
            status,
        )

    def _select_initial_step(
        self,
        term: Optional[ODETerm],
        t0: TimeTensor,
        y0: DataTensor,
        direction: torch.Tensor,
        dt_max: TimeTensor,
        convergence_order: int,
        stats: Dict[str, Any],
        args: Any,
    ) -> Tuple[TimeTensor, DataTensor]:
        """Empirically select a good initial step.

        This is an adaptation of the algorithm described in [1]_. We changed it in such a
        way that the tolerances apply to the norms instead of the components of `y`.

        References
        ----------
        .. [1] E. Hairer, S. P. Norsett G. Wanner, "Solving Ordinary Differential Equations
        I: Nonstiff Problems", Sec. II.4, 2nd edition.
        """

        if torch.jit.is_scripting() or term is None:
            assert term is None, "The integration term is fixed for JIT compilation"
            term = self.term
        assert term is not None

        norm = self.norm
        f0 = term.vf(t0, y0, stats, args)

        error_bounds = torch.add(self.atol, torch.abs(y0), alpha=self.rtol)
        inv_scale = torch.reciprocal(error_bounds)

        d0 = norm(y0 * inv_scale)
        d1 = norm(f0 * inv_scale)

        small_number = torch.tensor(1e-6, dtype=d0.dtype, device=d0.device)
        dt0 = torch.where((d0 < 1e-5) | (d1 < 1e-5), small_number, 0.01 * d0 / d1)

        # Ensure that we don't step out of the integration bounds
        dt0 = torch.minimum(dt0, dt_max.to(dtype=y0.dtype))

        y1 = torch.addcmul(y0, (direction * dt0)[:, None], f0)
        f1 = term.vf(
            torch.addcmul(t0, direction.to(dtype=t0.dtype), dt0.to(dtype=t0.dtype)),
            y1,
            stats,
            args,
        )

        d2 = norm((f1 - f0) * inv_scale) / dt0

        maxd1d2 = torch.maximum(d1, d2)
        dt1 = torch.where(
            maxd1d2 <= 1e-15,
            torch.maximum(small_number, dt0 * 1e-3),
            (0.01 / maxd1d2) ** (1.0 / convergence_order),
        )

        return (direction * torch.minimum(100 * dt0, dt1)).to(dtype=t0.dtype), f0
