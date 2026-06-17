(function () {
  function renderMermaidBlocks() {
    if (!window.mermaid) {
      return;
    }

    document.querySelectorAll("pre > code.language-mermaid").forEach(function (block) {
      var parent = block.parentElement;
      var container = document.createElement("div");
      container.className = "mermaid";
      container.textContent = block.textContent;
      parent.replaceWith(container);
    });

    window.mermaid.initialize({
      startOnLoad: true,
      theme: "base",
      themeVariables: {
        primaryColor: "#ffffcc",
        primaryBorderColor: "#000080",
        primaryTextColor: "#000000",
        lineColor: "#800000",
        secondaryColor: "#e6e6e6",
        tertiaryColor: "#ffffff"
      }
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", renderMermaidBlocks);
  } else {
    renderMermaidBlocks();
  }
})();
