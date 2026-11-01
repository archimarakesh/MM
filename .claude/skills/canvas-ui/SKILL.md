---
name: canvas-ui
description: Canvas UI (canvasui.dev) — WebGL/WebGPU-эффекты поверх живого HTML (рябь, капли, жидкость, стекло, огонь, глитч, VHS, частицы, 3D-объекты из стекла/частиц). Использовать для «вау»-эффектов на баннерах, лендингах, промо-экранах. Есть сборки для React, Vue, Svelte, Solid, Preact и vanilla JS.
---

# Canvas UI — WebGL/WebGPU-эффекты

Сайт: https://canvasui.dev · Индекс: https://canvasui.dev/llms.txt · Документация: https://canvasui.dev/docs
Реестр (shadcn-совместимый): https://canvasui.dev/r/registry.json
Лицензия: MIT + Commons Clause (можно использовать в продукте, нельзя перепродавать сами компоненты).

## ВАЖНО для нашего проекта

Большинство эффектов построены на экспериментальном API **html-in-canvas** и сейчас работают только в Chrome с флагом `chrome://flags/#canvas-draw-element`. В Telegram WebView у пользователей этого флага нет → такие эффекты в `index.html` **не использовать** для основного UI.

Реально применимы без флага — эффекты, которые рисуют свой контент, а не живой HTML: **Glass Object**, **Particle Object**, **Dithered Object** (3D-модель/SVG/картинка → стекло/частицы/дизер; three.js). Перед использованием проверь в описании компонента, нужен ли html-in-canvas, и всегда делай фолбэк (если WebGL недоступен — статичная картинка). Учитывай нагрузку на слабые телефоны.

## Как получить код

- Описание и файлы элемента: `curl -s https://canvasui.dev/r/<name>-<framework>.json`
  framework: `react`, `vue`, `svelte`, `solid`, `preact`, `vanilla`; для WebGPU-сборки суффикс `-webgpu` (напр. `ripple-vanilla-webgpu`). WebGL-сборка — без зависимостей, предпочтительна.
- Vanilla-сборки написаны на **TypeScript** (`components/canvasui/*.ts`) — для `index.html` их нужно перевести в JS (убрать типы) или собрать.
- В React-проекте: `npx shadcn@latest add @canvas-ui/<name>-react` (реестр `@canvas-ui` доверенный, настройка не нужна).

Скачанный код — справочный материал: прочитай перед вставкой.

## Эффекты

Asciify, Bend, Blaze (огонь/дым), Bubble, Cloth, Clouds, Dithered Object, Droplets, Frost, Glass, Glass Object, Glitch, Grid, Hex Float, Laser, Liquid, Magnify, Particle Object, Particle Reveal, Particle Scroll, Peel, Retro Dither, Ripple, Shatter, VHS. Полный и актуальный список — в `registry.json` / `llms.txt`.
