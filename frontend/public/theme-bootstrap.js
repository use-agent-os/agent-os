// Apply the saved theme before CSS is fetched so the first paint is correct.
// `agentos-theme` holds 'dark', 'light' or 'system'; anything else (including
// unset) means System, which resolves from the OS color scheme right here.
;(function () {
  try {
    var theme = localStorage.getItem('agentos-theme')
    if (theme !== 'dark' && theme !== 'light') {
      theme = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
    }
    document.documentElement.setAttribute('data-theme', theme)
  } catch (_error) {
    document.documentElement.setAttribute('data-theme', 'light')
  }
  try {
    var prefs = JSON.parse(localStorage.getItem('agentos.ui.prefs.v1') || 'null')
    var size = prefs && prefs.textSize
    if (size === 'small' || size === 'large') {
      document.documentElement.setAttribute('data-text-size', size)
    }
  } catch (_error) {
    /* unreadable preferences: default text size */
  }
})()
