import { useEffect, useRef, useState } from "react"
import { createPortal } from "react-dom"
import { Toaster as Sonner } from "sonner"

function ToastSurface() {
  const hostRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const host = hostRef.current
    if (!host) return
    const keepTextDragInsideContent = (event: Event) => event.stopPropagation()
    const enableTextSelection = () => {
      // Sonner captures pointer drags on the toast for swipe-to-dismiss. Stop
      // that handler at text content so the browser can create a selection.
      host.querySelectorAll("[data-title], [data-description]").forEach((element) => {
        element.addEventListener("pointerdown", keepTextDragInsideContent)
      })
    }
    const observer = new MutationObserver(enableTextSelection)
    observer.observe(host, { childList: true, subtree: true })
    enableTextSelection()
    return () => {
      observer.disconnect()
      host.querySelectorAll("[data-title], [data-description]").forEach((element) => {
        element.removeEventListener("pointerdown", keepTextDragInsideContent)
      })
    }
  }, [])

  return <div ref={hostRef} className="pointer-events-none fixed inset-0 z-[70]">
    <Sonner
      richColors
      closeButton
      position="bottom-right"
      swipeDirections={[]}
      toastOptions={{
        className: "font-sans select-text",
        style: { pointerEvents: "auto", userSelect: "text", WebkitUserSelect: "text", touchAction: "auto" },
        classNames: { title: "select-text", description: "select-text" },
      }}
    />
  </div>
}

export function Toaster() {
  const [portalContainer] = useState(() => document.createElement("div"))

  useEffect(() => {
    const moveIntoActiveDialog = () => {
      const dialogs = document.querySelectorAll<HTMLElement>('[role="dialog"][data-state="open"]')
      const parent = dialogs.item(dialogs.length - 1) ?? document.body
      if (portalContainer.parentElement !== parent) parent.appendChild(portalContainer)
    }
    // Radix blocks interaction outside a modal dialog. Keep the stable toast
    // portal inside that scope so notifications remain actionable/selectable
    // without remounting Sonner and losing an in-flight notification.
    const observer = new MutationObserver(moveIntoActiveDialog)
    observer.observe(document.body, { childList: true, subtree: true })
    moveIntoActiveDialog()
    return () => {
      observer.disconnect()
      portalContainer.remove()
    }
  }, [portalContainer])

  return createPortal(<ToastSurface />, portalContainer)
}
