document.documentElement.classList.add("js")

const themeToggle = document.querySelector("#theme-toggle")
const themeLabel = themeToggle?.querySelector(".theme-label")
const copyStatus = document.querySelector("#copy-status")
const storedThemeKey = "posetestbot-docs-theme"

function readStoredTheme() {
  try {
    const value = window.localStorage.getItem(storedThemeKey)
    return value === "light" || value === "dark" ? value : null
  } catch {
    return null
  }
}

function preferredTheme() {
  return window.matchMedia("(prefers-color-scheme: dark)").matches
    ? "dark"
    : "light"
}

function activeTheme() {
  return document.documentElement.dataset.theme || preferredTheme()
}

function updateThemeControl() {
  if (!themeToggle) return

  const current = activeTheme()
  const next = current === "dark" ? "light" : "dark"
  themeToggle.setAttribute("aria-pressed", String(current === "dark"))
  themeToggle.setAttribute("aria-label", `Switch to ${next} theme`)
  if (themeLabel) themeLabel.textContent = current === "dark" ? "Dark" : "Light"
}

const storedTheme = readStoredTheme()
if (storedTheme) document.documentElement.dataset.theme = storedTheme
updateThemeControl()

themeToggle?.addEventListener("click", () => {
  const next = activeTheme() === "dark" ? "light" : "dark"
  document.documentElement.dataset.theme = next
  try {
    window.localStorage.setItem(storedThemeKey, next)
  } catch {
    // The chosen theme still applies for this page view when storage is blocked.
  }
  updateThemeControl()
})

window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
  if (!document.documentElement.dataset.theme) updateThemeControl()
})

for (const button of document.querySelectorAll("[data-copy-target]")) {
  button.addEventListener("click", async () => {
    const targetId = button.getAttribute("data-copy-target")
    const target = targetId ? document.getElementById(targetId) : null
    if (!target) return

    const commands = target.textContent?.trim() || ""
    try {
      await navigator.clipboard.writeText(commands)
      button.textContent = "Copied"
      if (copyStatus) copyStatus.textContent = "Quick-start commands copied."
      window.setTimeout(() => {
        button.textContent = "Copy commands"
      }, 1800)
    } catch {
      if (copyStatus) {
        copyStatus.textContent =
          "Copying was unavailable. Select the commands in the code block instead."
      }
    }
  })
}
