import { useEffect, useMemo, useState } from "react"
import { useSearchParams } from "react-router-dom"
import { useOperator } from "@/providers/operator-provider"

export function useInspectionSearch() {
  const { selectedRunEpoch } = useOperator()
  const [boundEpoch, setBoundEpoch] = useState(selectedRunEpoch)
  const [params, setParams] = useSearchParams()
  const cleared = useMemo(() => new URLSearchParams(), [])
  const switched = selectedRunEpoch !== boundEpoch
  useEffect(() => {
    if (switched) {
      setParams(cleared, { replace: true })
      queueMicrotask(() => setBoundEpoch(selectedRunEpoch))
    }
  }, [cleared, selectedRunEpoch, setParams, switched])
  return { params: switched ? cleared : params, setParams, switched }
}
