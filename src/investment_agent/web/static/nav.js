// 窄屏左侧抽屉导航：同一个 <nav> 在宽屏是顶栏横排，窄屏是抽屉。
// 脚本未执行时（无 nav-js 类）窄屏回退为顶栏下方平铺，链接始终可达。
(function () {
  var root = document.documentElement;
  root.classList.add("nav-js");

  function init() {
    var toggle = document.querySelector(".nav-toggle");
    var nav = document.getElementById("app-nav");
    var scrim = document.querySelector(".nav-scrim");
    var closeBtn = document.querySelector(".nav-close");
    var title = document.querySelector(".topbar-title");
    if (!toggle || !nav) return;

    var active = nav.querySelector("a.active span");
    if (title && active) title.textContent = active.textContent;

    var mq = window.matchMedia("(max-width: 40rem)");

    function setOpen(open) {
      root.classList.toggle("nav-open", open);
      toggle.setAttribute("aria-expanded", String(open));
      if (open) {
        (nav.querySelector("a.active") || closeBtn).focus();
      } else if (nav.contains(document.activeElement)) {
        toggle.focus();
      }
    }

    toggle.addEventListener("click", function () { setOpen(true); });
    if (closeBtn) closeBtn.addEventListener("click", function () { setOpen(false); });
    if (scrim) scrim.addEventListener("click", function () { setOpen(false); });

    document.addEventListener("keydown", function (e) {
      if (!root.classList.contains("nav-open")) return;
      if (e.key === "Escape") {
        setOpen(false);
      } else if (e.key === "Tab") {
        var items = nav.querySelectorAll("a[href], button");
        var first = items[0];
        var last = items[items.length - 1];
        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault();
          last.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault();
          first.focus();
        }
      }
    });

    var onChange = function () { if (!mq.matches) setOpen(false); };
    if (mq.addEventListener) mq.addEventListener("change", onChange);
    else mq.addListener(onChange);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
