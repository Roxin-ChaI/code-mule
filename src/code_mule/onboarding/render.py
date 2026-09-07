"""Line-oriented rendering for doctor and start preflight results."""

from .contracts import DoctorReport


def render_doctor(report: DoctorReport, *, verbose: bool = False) -> tuple[str, ...]:
    """Render doctor rows without credentials or raw command output."""

    lines: list[str] = ["DOCTOR", ""]
    for check in report.checks:
        lines.append(f"{check.label:<12}{check.status}")
        if verbose and check.details:
            lines.extend(f"{'':<12}{detail}" for detail in check.details)
        if not check.healthy and check.advice:
            lines.extend(f"{'':<12}{advice}" for advice in check.advice)
    lines.append("")
    if report.healthy:
        lines.append("ALL CHECKS PASSED")
    else:
        lines.append("ENVIRONMENT NOT READY")
        count = report.problem_count
        lines.append(f"{count} check{'s' if count != 1 else ''} need attention.")
    return tuple(lines)


__all__ = ["render_doctor"]
