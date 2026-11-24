import { RefreshCw } from "lucide-react"
import { Button } from "@/components/ui/button"
import { errorMessage } from "@/lib/api"

export function QueryProblem({ title, error, retry, retrying = false }: { title: string; error: unknown; retry: () => void; retrying?: boolean }) {
  return <div role="alert" className="flex flex-wrap items-start justify-between gap-3 rounded-lg border border-destructive/30 bg-destructive/5 p-3">
    <div className="min-w-0"><h2 className="text-sm font-semibold">{title}</h2><p className="mt-1 break-words text-xs text-muted-foreground">{errorMessage(error)}</p></div>
    <Button variant="outline" size="sm" onClick={retry} disabled={retrying}><RefreshCw className={retrying ? "animate-spin" : undefined} />Retry</Button>
  </div>
}
