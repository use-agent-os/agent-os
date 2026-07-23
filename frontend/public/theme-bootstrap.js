// Apply the saved theme before CSS is fetched so the first paint is correct.
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
})()
