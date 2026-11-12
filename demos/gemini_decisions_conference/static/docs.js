/* Interactive Medium Publishing Helpers & Accessible Theme Toggle for /docs
 * Strictly uses safe DOM APIs and XMLSerializer with zero unsafe markup sinks.
 */

(function () {
  "use strict";

  const STORAGE_KEY = "docs-color-scheme";
  const CANONICAL_URL = "https://gemini-decisions-studio-1085727639300.us-central1.run.app/docs";

  const toastEl = document.getElementById("copyToast");
  const copyRichBtn = document.getElementById("copyRichMediumBtn");
  const copyMdBtn = document.getElementById("copyMarkdownBtn");
  const copyUrlBtn = document.getElementById("copyImportUrlBtn");
  const themeBtn = document.getElementById("themeToggleBtn");
  const articleEl = document.getElementById("mediumArticleBody");
  const markdownSourceEl = document.getElementById("mediumMarkdownSource");
  const schemeMeta = document.querySelector('meta[name="color-scheme"]');
  const darkMql = window.matchMedia("(prefers-color-scheme: dark)");

  let toastTimer = null;

  function showToast(message) {
    if (!toastEl) {
      return;
    }
    toastEl.textContent = message;
    toastEl.setAttribute("data-visible", "true");
    if (toastTimer) {
      window.clearTimeout(toastTimer);
    }
    toastTimer = window.setTimeout(function () {
      toastEl.setAttribute("data-visible", "false");
    }, 2800);
  }

  function getEffectiveScheme() {
    const pinned = document.documentElement.getAttribute("data-theme");
    if (pinned === "light" || pinned === "dark") {
      return pinned;
    }
    return darkMql.matches ? "dark" : "light";
  }

  function applyScheme(pinnedValue) {
    if (pinnedValue === "light" || pinnedValue === "dark") {
      document.documentElement.setAttribute("data-theme", pinnedValue);
      if (schemeMeta) {
        schemeMeta.setAttribute("content", pinnedValue);
      }
      try {
        window.localStorage.setItem(STORAGE_KEY, pinnedValue);
      } catch (_err) {
        // Ignore storage errors in private browsing
      }
    } else {
      document.documentElement.removeAttribute("data-theme");
      if (schemeMeta) {
        schemeMeta.setAttribute("content", "light dark");
      }
      try {
        window.localStorage.removeItem(STORAGE_KEY);
      } catch (_err) {
        // Ignore storage errors
      }
    }
    updateThemeButtonLabel();
  }

  function updateThemeButtonLabel() {
    if (!themeBtn) {
      return;
    }
    const effective = getEffectiveScheme();
    themeBtn.textContent = effective === "dark" ? "☀ Light Mode" : "🌙 Dark Mode";
  }

  if (themeBtn) {
    updateThemeButtonLabel();
    darkMql.addEventListener("change", updateThemeButtonLabel);
    themeBtn.addEventListener("click", function () {
      const systemScheme = darkMql.matches ? "dark" : "light";
      const currentPinned = document.documentElement.getAttribute("data-theme");
      const oppositeOfSystem = systemScheme === "dark" ? "light" : "dark";
      if (!currentPinned) {
        applyScheme(oppositeOfSystem);
      } else if (currentPinned === oppositeOfSystem) {
        applyScheme(null);
      } else {
        applyScheme(oppositeOfSystem);
      }
    });
  }

  function buildCleanMediumHtml() {
    if (!articleEl) {
      return "";
    }
    const clone = articleEl.cloneNode(true);
    const excludeNodes = clone.querySelectorAll("[data-medium-exclude='true']");
    excludeNodes.forEach(function (node) {
      node.remove();
    });
    const allNodes = clone.querySelectorAll("*");
    allNodes.forEach(function (el) {
      el.removeAttribute("class");
      el.removeAttribute("id");
      el.removeAttribute("loading");
      el.removeAttribute("fetchpriority");
    });
    clone.removeAttribute("id");
    clone.removeAttribute("class");
    return new XMLSerializer().serializeToString(clone);
  }

  if (copyRichBtn) {
    copyRichBtn.addEventListener("click", async function () {
      if (!articleEl) {
        return;
      }
      const cleanMarkup = buildCleanMediumHtml();
      const plainText = articleEl.innerText || articleEl.textContent || "";
      try {
        if (window.ClipboardItem && navigator.clipboard && navigator.clipboard.write) {
          const item = new window.ClipboardItem({
            "text/html": new Blob([cleanMarkup], { type: "text/html" }),
            "text/plain": new Blob([plainText], { type: "text/plain" }),
          });
          await navigator.clipboard.write([item]);
          showToast("Copied rich Medium story! Open medium.com/new-story and press Cmd+V / Ctrl+V.");
          return;
        }
        await navigator.clipboard.writeText(plainText);
        showToast("Copied story text to clipboard.");
      } catch (_err) {
        showToast("Clipboard permission blocked — use Medium Import URL instead.");
      }
    });
  }

  if (copyMdBtn && markdownSourceEl) {
    copyMdBtn.addEventListener("click", async function () {
      const md = (markdownSourceEl.textContent || "").trim();
      try {
        await navigator.clipboard.writeText(md);
        showToast("Copied full Markdown article to clipboard!");
      } catch (_err) {
        showToast("Unable to copy Markdown automatically.");
      }
    });
  }

  if (copyUrlBtn) {
    copyUrlBtn.addEventListener("click", async function () {
      try {
        await navigator.clipboard.writeText(CANONICAL_URL);
        showToast("Copied /docs URL! Paste it at medium.com/p/import.");
      } catch (_err) {
        showToast(CANONICAL_URL);
      }
    });
  }
})();
