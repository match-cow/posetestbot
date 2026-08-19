import { useEffect } from "react"
import { useQuery } from "@tanstack/react-query"
import { Link, NavLink, Outlet, useLocation } from "react-router-dom"
import { ArrowRight, BookOpen, Bot, Boxes, ChartNoAxesCombined, Check, Circle, CircleDot, Cpu, FolderOpen, Folders, Gauge, Grid3X3, LayoutTemplate, ListChecks, LoaderCircle, LockKeyhole, Moon, PackageSearch, Route, Sun, Workflow } from "lucide-react"
import { RestartControl } from "@/components/restart-control"
import { Button } from "@/components/ui/button"
import { Select, SelectContent, SelectItem, SelectTrigger } from "@/components/ui/select"
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip"
import { useOperator } from "@/providers/operator-provider"
import { useTheme } from "@/providers/theme-provider"
import { api, query } from "@/lib/api"
import type { CaptureState, Job } from "@/lib/contracts"
import { cn } from "@/lib/utils"
import { activeWorkflowHref, type ActiveWorkflow, type WorkflowProgressStatus } from "@/lib/workflow-session"
import { toast } from "sonner"

const navigationGroups = [
  {
    label: "Operate",
    items: [
      { to: "/dashboard", label: "Dashboard", icon: Gauge },
      { to: "/workflow/setup", label: "Workflow", icon: Workflow, match: "/workflow" },
    ],
  },
  {
    label: "Prepare",
    items: [
      { to: "/devices", label: "Devices", icon: Bot },
      { to: "/calibration-targets", label: "Calibration Targets", icon: Grid3X3 },
      { to: "/workpieces", label: "Workpiece Catalogue", icon: PackageSearch },
      { to: "/pose-templates", label: "Pose Templates", icon: LayoutTemplate },
    ],
  },
  {
    label: "Inspect",
    items: [
      { to: "/cell", label: "Cell View", icon: Boxes },
      { to: "/run-folders", label: "Run folders", icon: Folders },
      { to: "/pose-estimation", label: "Pose Estimation", icon: Cpu },
      { to: "/bop-evaluation", label: "BOP Evaluation", icon: ChartNoAxesCombined },
      { to: "/jobs", label: "Jobs", icon: ListChecks },
    ],
  },
]
const navigation = navigationGroups.flatMap((group) => group.items)

const workflowStatusPresentation: Record<WorkflowProgressStatus, { label: string; className: string; icon: typeof CircleDot }> = {
  complete: { label: "Complete", className: "border-success/30 bg-success/10 text-success", icon: Check },
  current: { label: "Current step", className: "border-primary/35 bg-primary/10 text-primary-strong", icon: CircleDot },
  ready: { label: "Ready", className: "border-primary/35 bg-primary/10 text-primary-strong", icon: CircleDot },
  blocked: { label: "Needs attention", className: "border-destructive/30 bg-destructive/10 text-destructive", icon: LockKeyhole },
  running: { label: "Running", className: "border-warning/35 bg-warning/10 text-warning-foreground", icon: LoaderCircle },
  not_started: { label: "Not started", className: "border-sidebar-border bg-secondary text-sidebar-foreground/60", icon: Circle },
}

interface WorkflowRuntimeStatus {
  value: string
  label: string
}

function workflowRuntimePresentation(runtime: WorkflowRuntimeStatus) {
  if (["failed", "canceled", "cancelled"].includes(runtime.value)) {
    return { label: runtime.label, className: "border-destructive/30 bg-destructive/10 text-destructive", icon: LockKeyhole }
  }
  if (runtime.value === "succeeded") {
    return { label: runtime.label, className: "border-success/30 bg-success/10 text-success", icon: Check }
  }
  return { label: runtime.label, className: "border-warning/35 bg-warning/10 text-warning-foreground", icon: LoaderCircle }
}

function CurrentWorkflowCard({ workflow, runtime }: { workflow: ActiveWorkflow; runtime?: WorkflowRuntimeStatus | null }) {
  const status = runtime ? workflowRuntimePresentation(runtime) : workflowStatusPresentation[workflow.status]
  const StatusIcon = status.icon
  const href = activeWorkflowHref(workflow)
  return <section data-testid="current-workflow-card" aria-label="Workflow resume position for active run" className="rounded-[10px] border border-primary/35 bg-primary/5 p-3">
    <div className="flex items-start justify-between gap-2">
      <div className="text-[9px] font-bold uppercase tracking-[0.15em] text-primary-strong">Resume position</div>
      <span role="status" className={cn("inline-flex shrink-0 items-center gap-1 rounded-full border px-1.5 py-0.5 text-[9px] font-semibold", status.className)}>
        <StatusIcon aria-hidden="true" className={cn("size-2.5", (workflow.status === "running" || runtime && ["queued", "running", "canceling"].includes(runtime.value)) && "animate-spin")} />
        {status.label}
      </span>
    </div>
    <div className="mt-2 text-xs font-semibold">{workflow.journeyTitle}</div>
    <div className="mt-1 text-[10px] font-bold uppercase tracking-wider text-sidebar-foreground/45">Active run · Viewed step {workflow.stepNumber} of {workflow.stepCount}</div>
    <div className="mt-1 text-[11px] leading-snug text-sidebar-foreground/70">{workflow.stepTitle}</div>
    <Button asChild size="sm" className="mt-3 h-8 w-full text-xs">
      <Link to={href} aria-label={`Resume ${workflow.journeyTitle.toLowerCase()} at step ${workflow.stepNumber}: ${workflow.stepTitle}`}>Resume step {workflow.stepNumber}<ArrowRight aria-hidden="true" /></Link>
    </Button>
    <Link to="/workflow/setup" className="mt-2 block text-center text-[10px] font-semibold text-sidebar-foreground/55 underline-offset-4 hover:text-sidebar-foreground hover:underline">Choose another workflow</Link>
  </section>
}

export function AppShell() {
  const { bootstrap, runs, selectedRun, selectRun, currentWorkflow } = useOperator()
  const { theme, setTheme } = useTheme()
  const location = useLocation()
  const workflowHref = currentWorkflow ? activeWorkflowHref(currentWorkflow) : "/workflow/setup"
  const activeRun = runs.find((run) => run.path === selectedRun)
  const activeFolderName = selectedRun.split("/").filter(Boolean).at(-1) ?? selectedRun
  const activeRunName = activeRun?.run_name ?? activeFolderName
  const runOptions = activeRun
    ? runs
    : [{
        path: selectedRun,
        name: activeFolderName,
        run_name: null,
        run_id: null,
        intent: null,
        annotation_mode: null,
        config_valid: false,
        config_error: null,
        modified_at: "",
      }, ...runs]
  const captureState = useQuery({
    queryKey: ["capture-jobs", selectedRun],
    queryFn: () => api<CaptureState>(query("/capture/jobs", { run_root: selectedRun })),
    enabled: currentWorkflow?.stepId === "capture",
    refetchInterval: (state) => state.state.data?.active_count ? 1_000 : 5_000,
  })
  const processingJobs = useQuery({
    queryKey: ["jobs"],
    queryFn: () => api<{ jobs: Job[]; resources: Record<string, string> }>("/jobs"),
    enabled: currentWorkflow?.journey === "dataset" && currentWorkflow.stepId === "sync",
    refetchInterval: (state) => state.state.data?.jobs.some((job) => ["queued", "running", "canceling"].includes(job.status)) ? 1_000 : 5_000,
  })
  const activeCapture = currentWorkflow?.stepId === "capture"
    ? captureState.data?.jobs.find((job) => job.active)
    : undefined
  const datasetProcessingJob = currentWorkflow?.journey === "dataset" && currentWorkflow.stepId === "sync"
    ? [...(processingJobs.data?.jobs ?? [])]
        .filter((job) => job.scope_kind === "run"
          && job.run_root === selectedRun
          && job.parameters.purpose === "dataset_processing")
        .sort((left, right) => right.created_at.localeCompare(left.created_at))[0]
    : undefined
  const workflowRuntimeStatus: WorkflowRuntimeStatus | null = activeCapture
    ? {
        value: activeCapture.status,
        label: activeCapture.status === "queued"
          ? "Recording queued"
          : activeCapture.status === "canceling"
            ? "Recording stopping"
            : "Recording running",
      }
    : datasetProcessingJob
      ? {
          value: datasetProcessingJob.status,
          label: datasetProcessingJob.status === "queued"
            ? "Processing queued"
            : datasetProcessingJob.status === "running"
              ? "Processing running"
              : datasetProcessingJob.status === "canceling"
                ? "Processing stopping"
                : datasetProcessingJob.status === "succeeded"
                  ? "Processing finished"
                  : ["canceled", "cancelled"].includes(datasetProcessingJob.status)
                    ? "Processing canceled"
                    : "Processing failed",
        }
      : null

  useEffect(() => {
    window.scrollTo({ top: 0, left: 0, behavior: "auto" })
  }, [location.pathname])

  const switchRun = (path: string) => {
    if (path === selectedRun) return
    const nextRun = runs.find((run) => run.path === path)
    if (!selectRun(path)) {
      toast.error("Run folder must stay inside an allowed storage root")
      return
    }
    toast.success("Active run changed", {
      description: `${nextRun?.run_name ?? nextRun?.name ?? path} is now used by every run-owned page and action.`,
    })
  }

  return (
    <TooltipProvider delayDuration={150}>
      <div className="min-h-screen bg-workspace text-foreground">
        <aside
          aria-label="Application sidebar"
          className="fixed inset-y-0 left-0 z-40 hidden w-[244px] flex-col overflow-y-auto border-r border-sidebar-border bg-sidebar px-4 py-5 text-sidebar-foreground xl:flex"
        >
          <Link to="/dashboard" className="flex items-center gap-3 px-2">
            <img src={bootstrap.brand.logo_urls[theme]} alt={bootstrap.brand.name} className="size-9 rounded-[7px] object-contain" />
            <div><div className="font-display text-[17px] font-semibold tracking-tight">PoseTestBot</div><div className="text-[9px] font-bold uppercase tracking-[0.18em] text-sidebar-foreground/50">Operator console</div></div>
          </Link>
          <nav className="mt-7 space-y-5" aria-label="Primary navigation">
            {navigationGroups.map((group) => <div key={group.label}>
              <div className="mb-1.5 px-3 text-[9px] font-bold uppercase tracking-[0.16em] text-sidebar-foreground/40">{group.label}</div>
              <div className="space-y-1">{group.items.map(({ to, label, icon: Icon, match }) => {
                const active = match ? location.pathname.startsWith(match) : location.pathname === to
                const destination = match === "/workflow" ? workflowHref : to
                return <NavLink key={to} to={destination} className={cn("group flex items-center gap-3 rounded-[8px] border border-transparent px-3 py-2 text-[13px] font-semibold text-sidebar-foreground/65 transition-colors duration-150 hover:bg-secondary hover:text-sidebar-foreground", active && "border-primary/55 bg-sidebar-accent text-sidebar-foreground")}><Icon className={cn("size-[17px]", active && "text-primary-strong")} />{label}</NavLink>
              })}</div>
            </div>)}
          </nav>
          <div className="mt-auto space-y-2 pt-5">
            {currentWorkflow ? <CurrentWorkflowCard workflow={currentWorkflow} runtime={workflowRuntimeStatus} /> : <Link to="/workflow/setup" className="block rounded-[10px] border border-primary/30 bg-primary/5 p-3 transition-colors hover:bg-primary/10">
              <div className="flex items-center gap-2 text-xs font-semibold"><Route className="size-4 text-primary-strong" />Guided acquisition</div>
              <div className="mt-1 text-[10px] leading-relaxed text-sidebar-foreground/55">Start or resume the required operator path.</div>
            </Link>}
          </div>
        </aside>

        <div className="min-w-0 xl:ml-[244px]">
          <header className="sticky top-0 z-30 border-b border-border bg-card/95 py-2 backdrop-blur-xl xl:h-14 xl:py-2">
            <div className="mx-auto flex h-full max-w-[1600px] flex-wrap items-center justify-between gap-3 px-4 sm:px-5 xl:px-7">
              <div className="flex min-w-0 flex-1 items-center gap-2 sm:gap-3">
                <Link to="/dashboard" className="shrink-0 xl:hidden" aria-label="Open dashboard">
                  <img src={bootstrap.brand.logo_urls[theme]} alt="" className="size-8 rounded-[7px] object-contain" />
                </Link>
                <section
                  aria-label="Active run context"
                  className="flex h-10 min-w-0 flex-1 items-stretch gap-2 xl:max-w-[1000px]"
                  data-testid="active-run-context"
                >
                  <Select value={selectedRun} onValueChange={switchRun}>
                    <SelectTrigger
                      aria-label="Active run folder"
                      className="h-full min-w-0 flex-1 justify-start gap-2 bg-muted/35 px-3 text-left shadow-sm [&>svg:last-child]:ml-auto"
                      data-testid="active-run-switcher"
                      title={selectedRun}
                    >
                      <div className="flex min-w-0 flex-1 items-center gap-2 overflow-hidden">
                        <span className="grid size-7 shrink-0 place-items-center rounded-md bg-primary/10"><FolderOpen className="size-4 text-primary-strong" aria-hidden="true" /></span>
                        <span className="hidden shrink-0 text-[9px] font-bold uppercase tracking-[0.14em] text-primary-strong sm:inline">Active acquisition run</span>
                        <span className="hidden h-3.5 w-px shrink-0 bg-border sm:inline" aria-hidden="true" />
                        <strong className="max-w-[34%] shrink-0 truncate text-xs" data-testid="active-run-name">{activeRunName}</strong>
                        <span className="hidden shrink-0 text-[8px] font-bold uppercase tracking-wider text-muted-foreground md:inline">Folder</span>
                        <span className="hidden min-w-0 flex-1 truncate font-mono text-[9px] text-muted-foreground md:inline" data-testid="active-run-path">{selectedRun}</span>
                        <span className={cn("hidden shrink-0 rounded-full border px-1.5 py-0.5 text-[8px] font-bold uppercase tracking-wider lg:inline-flex", activeRun?.config_valid ? "border-success/30 bg-success/10 text-success" : "border-warning/35 bg-warning/10 text-warning-foreground")}>{activeRun?.config_valid ? "Configured" : "Not configured"}</span>
                      </div>
                    </SelectTrigger>
                    <SelectContent align="start" className="w-[var(--radix-select-trigger-width)] max-w-[calc(100vw-2rem)]" data-testid="active-run-options">
                      {runOptions.map((run) => <SelectItem key={run.path} value={run.path} className="py-2 pr-3">
                        <span className="flex min-w-0 flex-1 flex-col gap-0.5">
                          <span className="flex min-w-0 items-center gap-2">
                            <span className="truncate font-semibold">{run.run_name ?? run.name}</span>
                            <span className={cn("shrink-0 rounded-full border px-1.5 py-0.5 text-[8px] font-bold uppercase tracking-wider", run.config_valid ? "border-success/30 bg-success/10 text-success" : "border-warning/35 bg-warning/10 text-warning-foreground")}>{run.config_valid ? run.intent ?? "Configured" : "Not configured"}</span>
                          </span>
                          <span className="truncate font-mono text-[9px] text-muted-foreground">{run.path}</span>
                        </span>
                      </SelectItem>)}
                    </SelectContent>
                  </Select>
                  <Button asChild variant="outline" className="h-full shrink-0 rounded-lg px-3" data-testid="manage-run-folders">
                    <Link to="/run-folders" aria-label="Manage run folders"><Folders aria-hidden="true" /><span className="hidden sm:inline">Run folders</span></Link>
                  </Button>
                </section>
              </div>
              <div className="flex shrink-0 items-center gap-2">
                <Button asChild variant="outline" className="h-[34px]"><a href="https://match-cow.github.io/posetestbot/" target="_blank" rel="noreferrer" data-testid="documentation-link"><BookOpen />Documentation</a></Button>
                <Tooltip><TooltipTrigger asChild><Button variant="outline" size="icon" className="size-[34px]" onClick={() => setTheme(theme === "light" ? "dark" : "light")} aria-label={`Switch to ${theme === "light" ? "dark" : "light"} theme`}>{theme === "light" ? <Moon /> : <Sun />}</Button></TooltipTrigger><TooltipContent>{theme === "light" ? "Use dark theme" : "Use light theme"}</TooltipContent></Tooltip>
                <RestartControl />
              </div>
              <nav className="order-3 flex w-full gap-1 overflow-x-auto pb-0.5 xl:hidden" aria-label="Primary navigation">
                {navigation.map(({ to, label, icon: Icon, match }) => {
                  const active = match ? location.pathname.startsWith(match) : location.pathname === to
                  const destination = match === "/workflow" ? workflowHref : to
                  return <NavLink key={to} to={destination} className={cn("flex shrink-0 items-center gap-2 rounded-[8px] border border-transparent px-2.5 py-2 text-xs font-semibold text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground", active && "border-primary/55 bg-primary/10 text-foreground")}><Icon className={cn("size-4", active && "text-primary-strong")} />{label}</NavLink>
                })}
              </nav>
            </div>
          </header>
          <main className="mx-auto max-w-[1600px] p-4 sm:p-5 xl:p-7"><Outlet /></main>
        </div>
      </div>
    </TooltipProvider>
  )
}
