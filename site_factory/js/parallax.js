/* Живой фон первого экрана. Просит его сама секция: контракт hero_bg_photo
   объявляет js: parallax, и только тогда base/head.j2 ставит
   <script defer src="/assets/parallax.js"> (engine/render.scripts_for).

   Красное правило проекта: большая картинка на странице бывает либо фоном
   секции, либо живой. Здесь она и то, и другое — снимок смещается от
   прокрутки и от указателя.

   Запас хода задан в разметке: картинка выше секции на 12% и поднята на 6%
   (sections/hero/hero_bg_photo.html.j2), поэтому сдвиг вниз-вверх не может
   обнажить край. Отсюда SHIFT ниже: доля высоты секции, меньшая этого запаса.

   Полный no-op при prefers-reduced-motion и на тач-экране: там фон просто
   стоит, и секция от этого не перестаёт быть секцией. */
(function () {
  "use strict";

  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  if (!window.matchMedia("(pointer: fine)").matches) return;

  var SHIFT = 0.05;       // доля высоты секции на всём пути прокрутки
  var MOUSE_PX = 14;      // размах от указателя, пиксели

  var layers = [];
  document.querySelectorAll("[data-parallax]").forEach(function (section) {
    var layer = section.querySelector("[data-parallax-layer]");
    if (layer) layers.push({ section: section, layer: layer, mouse: 0 });
  });
  if (!layers.length) return;

  var queued = false;

  function draw() {
    queued = false;
    var view = window.innerHeight;
    layers.forEach(function (item) {
      var box = item.section.getBoundingClientRect();
      if (box.bottom < 0 || box.top > view) return;
      // -1 — секция ниже кадра, +1 — выше него; 0 — её середина в середине окна.
      var progress = (box.top + box.height / 2 - view / 2) / view;
      var y = -Math.max(-1, Math.min(1, progress)) * box.height * SHIFT;
      item.layer.style.transform =
        "translate3d(" + item.mouse.toFixed(2) + "px, " + y.toFixed(2) + "px, 0)";
    });
  }

  function schedule() {
    if (queued) return;
    queued = true;
    requestAnimationFrame(draw);
  }

  layers.forEach(function (item) {
    item.section.addEventListener("mousemove", function (event) {
      var box = item.section.getBoundingClientRect();
      item.mouse = ((event.clientX - box.left) / box.width - 0.5) * -MOUSE_PX;
      schedule();
    }, { passive: true });
    item.section.addEventListener("mouseleave", function () {
      item.mouse = 0;
      schedule();
    }, { passive: true });
  });

  window.addEventListener("scroll", schedule, { passive: true });
  window.addEventListener("resize", schedule, { passive: true });
  schedule();
})();
