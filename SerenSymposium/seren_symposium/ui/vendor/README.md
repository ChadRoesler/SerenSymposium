# ui/vendor

Third-party files Symposium ships so the window renders with no network.
Served at `/vendor/...` and loaded by `ui/head.html`. Each keeps its own
licence header; update by replacing the file with the same build of a newer
version.

| file | what | version | licence | from |
|---|---|---|---|---|
| `marked.min.js` | Markdown to HTML | 12.0.2 | MIT | cdn.jsdelivr.net/npm/marked@12.0.2 |
| `purify.min.js` | DOMPurify: sanitises the HTML marked makes | 3.1.6 | Apache-2.0 / MPL-2.0 | cdn.jsdelivr.net/npm/dompurify@3.1.6 |
| `highlight.min.js` | highlight.js, the common-languages build | 11.9.0 | BSD-3-Clause | cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0 |
| `powershell.min.js` | highlight.js PowerShell grammar | 11.9.0 | BSD-3-Clause | same |
| `github-dark.min.css` | highlight.js theme | 11.9.0 | BSD-3-Clause | same |
