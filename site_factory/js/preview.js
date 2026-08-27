/* Движение и счётчик вовлечённости превью. Подключается ко всякой странице
   черновика: <script defer src="/assets/preview.js"> ставит base/head.j2.
   Инлайн-скриптов на превью нет и быть не может — CSP воркера разрешает
   только 'self' (worker/src/index.ts).

   Что делает файл:

   1. Плавный скролл — Lenis (/assets/lenis.js, вендор). Только на устройствах
      с точным указателем: на тач-экране прокрутку ведёт система, и вмешиваться
      в неё значит ломать инерцию, которую пользователь знает.
   2. Появление секций: [data-reveal] получает класс is-visible, когда входит
      в кадр. Сам эффект — CSS-переход в css/source.css, здесь только
      наблюдатель.
   3. Хиты view / scroll50 / dwell20 / cta_click на /api/hit — по одному
      разу каждый, через sendBeacon (запрос переживает уход со страницы).

   Начальное состояние появлений ставит скрипт, а не таблица стилей: не
   загрузился — страница просто видна целиком. Без JS превью полноценно.

   prefers-reduced-motion выключает пункты 1 и 2 целиком: ни Lenis, ни
   наблюдателя, ни атрибута data-motion. Хиты остаются — это измерение,
   а не движение, и терять на них половину визитов незачем. */
(function () {
  "use strict";

  var root = document.documentElement;
  var motionOk = !window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var finePointer = window.matchMedia("(pointer: fine)").matches;

  if (motionOk && finePointer && typeof window.Lenis === "function") {
    // autoRaf — собственный цикл кадров библиотеки; anchors — плавный переход
    // по ссылкам вида #cta, которые ставит композитор (engine/compose.py).
    new window.Lenis({ autoRaf: true, anchors: true });
  }

  if (motionOk && "IntersectionObserver" in window) {
    root.setAttribute("data-motion", "on");
    var seen = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (!entry.isIntersecting) return;
        entry.target.classList.add("is-visible");
        seen.unobserve(entry.target);
      });
    }, { threshold: 0.12, rootMargin: "0px 0px -8% 0px" });
    document.querySelectorAll("[data-reveal]").forEach(function (node) {
      seen.observe(node);
    });
  }

  var sent = {};

  function hit(event) {
    if (sent[event] || !navigator.sendBeacon) return;
    sent[event] = true;
    // Тело — только тип события: слаг воркер берёт из имени хоста, ни куки,
    // ни адреса здесь нет и не будет.
    navigator.sendBeacon("/api/hit", JSON.stringify({ event: event }));
  }

  hit("view");
  setTimeout(function () { hit("dwell20"); }, 20000);

  document.addEventListener("click", function (event) {
    if (event.target.closest("[data-hit='cta_click']")) hit("cta_click");
  }, { passive: true });

  window.addEventListener("scroll", function () {
    var scrolled = window.scrollY + window.innerHeight;
    if (scrolled >= document.documentElement.scrollHeight * 0.5) hit("scroll50");
  }, { passive: true });
})();
