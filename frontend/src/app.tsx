import { lazy, Suspense } from "react"
import { Navigate, Route, Routes } from "react-router-dom"
import { AppShell } from "@/components/app-shell"

const DashboardPage = lazy(() => import("@/features/dashboard/dashboard-page").then((module) => ({ default: module.DashboardPage })))
const DevicesPage = lazy(() => import("@/features/devices/devices-page").then((module) => ({ default: module.DevicesPage })))
const WorkflowPage = lazy(() => import("@/features/workflow/workflow-page").then((module) => ({ default: module.WorkflowPage })))
const JobsPage = lazy(() => import("@/features/jobs/jobs-page").then((module) => ({ default: module.JobsPage })))
const CellPage = lazy(() => import("@/features/cell/cell-page").then((module) => ({ default: module.CellPage })))
const CalibrationTargetsPage = lazy(() => import("@/features/calibration-targets/calibration-targets-page").then((module) => ({ default: module.CalibrationTargetsPage })))
const WorkpiecesPage = lazy(() => import("@/features/workpieces/workpieces-page").then((module) => ({ default: module.WorkpiecesPage })))
const PoseTemplatesPage = lazy(() => import("@/features/pose-templates/pose-templates-page").then((module) => ({ default: module.PoseTemplatesPage })))
const BopEvaluationPage = lazy(() => import("@/features/bop-evaluation/bop-evaluation-page").then((module) => ({ default: module.BopEvaluationPage })))
const PoseEstimationPage = lazy(() => import("@/features/pose-estimation/pose-estimation-page").then((module) => ({ default: module.PoseEstimationPage })))
const PoseResultsPage = lazy(() => import("@/features/pose-results/pose-results-page").then((module) => ({ default: module.PoseResultsPage })))
const RunFoldersPage = lazy(() => import("@/features/run-folders/run-folders-page").then((module) => ({ default: module.RunFoldersPage })))

function Page({ children }: { children: React.ReactNode }) {
  return <Suspense fallback={<div className="grid min-h-[60vh] place-items-center text-sm text-muted-foreground">Loading view…</div>}>{children}</Suspense>
}

export function App() {
  return (
    <Routes>
      <Route element={<AppShell />}>
        <Route index element={<Navigate to="/dashboard" replace />} />
        <Route path="/dashboard" element={<Page><DashboardPage /></Page>} />
        <Route path="/devices" element={<Page><DevicesPage /></Page>} />
        <Route path="/cell" element={<Page><CellPage /></Page>} />
        <Route path="/calibration-targets" element={<Page><CalibrationTargetsPage /></Page>} />
        <Route path="/workpieces" element={<Page><WorkpiecesPage /></Page>} />
        <Route path="/pose-templates" element={<Page><PoseTemplatesPage /></Page>} />
        <Route path="/bop-evaluation" element={<Page><BopEvaluationPage /></Page>} />
        <Route path="/pose-estimation" element={<Page><PoseEstimationPage /></Page>} />
        <Route path="/pose-results" element={<Page><PoseResultsPage /></Page>} />
        <Route path="/run-folders" element={<Page><RunFoldersPage /></Page>} />
        <Route path="/workflow" element={<Navigate to="/workflow/setup" replace />} />
        <Route path="/workflow/:phase" element={<Page><WorkflowPage /></Page>} />
        <Route path="/jobs" element={<Page><JobsPage /></Page>} />
        <Route path="*" element={<Navigate to="/dashboard" replace />} />
      </Route>
    </Routes>
  )
}
