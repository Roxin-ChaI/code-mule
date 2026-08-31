"""Explicit fail-closed errors for deterministic task scheduling."""


class SchedulerError(RuntimeError):
    """Base class for active-plan scheduling failures."""


class NoActivePlan(SchedulerError):
    """Raised when the project has no active plan identifier."""


class PlanStateInvalid(SchedulerError):
    """Raised when the active Plan or its Milestones are inconsistent."""


class TaskGraphInvalid(SchedulerError):
    """Raised when the active Plan's Task graph is ambiguous or empty."""


class NoRunnableTask(SchedulerError):
    """Raised by callers that require a Task when none is ready."""


class DependencyCycleDetected(TaskGraphInvalid):
    """Raised when the active Plan's dependency graph contains a cycle."""


class UnknownTaskDependency(TaskGraphInvalid):
    """Raised when a dependency is outside the active Plan Task graph."""


__all__ = [
    "DependencyCycleDetected",
    "NoActivePlan",
    "NoRunnableTask",
    "PlanStateInvalid",
    "SchedulerError",
    "TaskGraphInvalid",
    "UnknownTaskDependency",
]
