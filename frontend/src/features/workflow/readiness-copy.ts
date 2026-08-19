export interface ReadinessBlockerCopy {
  heading: string
  description: string
}

const blockerCopy: Record<string, ReadinessBlockerCopy> = {
  missing_preflight: {
    heading: "Readiness has not been checked",
    description: "Run the readiness check to create evidence for the current setup.",
  },
  stale_preflight: {
    heading: "Setup changed after the last check",
    description: "Camera, target, template, or run settings changed. Check readiness again for the current setup.",
  },
  failed_preflight: {
    heading: "A required readiness check failed",
    description: "Review the failed item, correct it, and run the readiness check again.",
  },
  invalid_preflight: {
    heading: "Readiness evidence cannot be read",
    description: "The saved evidence is incomplete or invalid. Run the readiness check again to replace it safely.",
  },
  missing_run_config: {
    heading: "Run setup is missing",
    description: "Save the camera and recording setup before checking readiness.",
  },
  readiness_incomplete: {
    heading: "Required readiness items are incomplete",
    description: "Complete the required setup items and run the readiness check before recording.",
  },
}

export function readinessBlockerCopy(blocker: string | null | undefined): ReadinessBlockerCopy {
  if (blocker === null) return {
    heading: "Readiness evidence is current",
    description: "The saved readiness evidence matches the current setup.",
  }
  if (blocker === undefined) return blockerCopy.missing_preflight
  return blockerCopy[blocker] ?? {
    heading: "Readiness needs attention",
    description: "Review the readiness step and run the check again before recording.",
  }
}
