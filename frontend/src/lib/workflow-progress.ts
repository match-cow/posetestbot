import type { BopAnnotationSetup, Overview, RunConfig } from "@/lib/contracts"
import type { WorkflowProgressStatus } from "@/lib/workflow-session"

export function artifactComplete(overview: Overview | undefined, path: string) {
  const item = overview?.sidebar.flatMap(section => section.artifacts).find(artifact => artifact.path === path)
  return Boolean(item?.exists && ["complete", "succeeded", "ok", "warning", "valid", "ready"].includes(item.status ?? ""))
}

export function workflowStepStatuses(completed: boolean[], annotation?: { config: RunConfig | null; setup?: BopAnnotationSetup; baseReady: boolean }): WorkflowProgressStatus[] {
  const firstIncomplete = completed.findIndex(value => !value)
  const steps: WorkflowProgressStatus[] = completed.map((value, index) => value ? "complete" : index === firstIncomplete ? "current" : "not_started")
  if (annotation) {
    // Ground truth is an optional post-capture addition: verified output of
    // either mode completes the step; otherwise it is available once the base
    // export exists.
    const output = annotation.setup?.current_output
    const done = Boolean(annotation.baseReady && output?.verified && (output.mode === "pose" || output.mode === "pose_and_masks"))
    steps.push(done ? "complete" : annotation.baseReady ? "ready" : "not_started")
  }
  return steps
}
