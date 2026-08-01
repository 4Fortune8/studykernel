// KaTeX, scoped. WEB_UI.md §5.2.
//
// Scoped rather than run over the document because two regions must be left
// exactly as the server wrote them:
//
//   * the briefing, which is copied verbatim into a chat client -- rendering
//     it would put HTML on the clipboard instead of the prompt;
//   * anything the learner typed, which is not maths and is not ours to
//     reinterpret.
//
// So only elements marked `.math` are rendered, and the panel is re-rendered
// after every htmx swap, because swapped-in HTML has never been through this.

(function () {
  var DELIMITERS = [
    { left: "$$", right: "$$", display: true },
    { left: "\\[", right: "\\]", display: true },
    { left: "\\(", right: "\\)", display: false },
    { left: "$", right: "$", display: false }
  ];

  function render(root) {
    if (!root || !window.renderMathInElement) return;
    var targets = root.matches && root.matches(".math") ? [root] : [];
    targets = targets.concat(Array.prototype.slice.call(root.querySelectorAll(".math")));
    targets.forEach(function (el) {
      if (el.dataset.mathDone === "1") return;
      window.renderMathInElement(el, {
        delimiters: DELIMITERS,
        // A malformed expression shows as source rather than taking the page
        // down with it. The corpus has 8,000 items and some of them will be
        // wrong; that is a content problem, not a reason for a blank screen.
        throwOnError: false,
        ignoredTags: ["script", "noscript", "style", "textarea", "pre", "code"]
      });
      el.dataset.mathDone = "1";
    });
  }

  document.addEventListener("DOMContentLoaded", function () { render(document.body); });
  document.body.addEventListener("htmx:afterSwap", function (event) { render(event.target); });
})();
