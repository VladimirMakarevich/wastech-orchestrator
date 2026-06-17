document.addEventListener("DOMContentLoaded", function () {
  document.querySelectorAll("pre").forEach(function (pre) {
    var btn = document.createElement("button");
    btn.className = "copy-btn";
    btn.textContent = "copy";

    btn.addEventListener("click", function () {
      var code = pre.querySelector("code");
      var text = code ? code.innerText : pre.innerText;
      navigator.clipboard.writeText(text).then(function () {
        btn.textContent = "copied!";
        setTimeout(function () {
          btn.textContent = "copy";
        }, 2000);
      });
    });

    pre.appendChild(btn);
  });
});
