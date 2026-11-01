---
name: devigner-ui
description: Бесплатные (MIT) анимированные React-компоненты Devigner UI (ui.devigner.cc) — табы, слайдеры, свитч, бейджи, док-меню, liquid glass, загрузка файлов, выбор дат, KPI-график, таймлайн, magnetic/flip/copy/delete-кнопки. Использовать для живых, не «серых» элементов интерфейса; в React-проекте — ставить через CLI, в ванильном index.html — как референс поведения и анимации.
---

# Devigner UI — анимированные компоненты

Сайт: https://ui.devigner.cc · Индекс: https://ui.devigner.cc/llms.txt · Полная документация: https://ui.devigner.cc/llms-full.txt
Лицензия MIT, всё бесплатно, включая коммерческое использование.

## Стек

React 18/19 + Tailwind CSS v4, анимации на Motion (бывш. Framer Motion), иногда GSAP. Цвета — токены shadcn/ui (`primary`, `muted`, `accent`, `border`…), тёмная тема через класс `.dark`.

- **React-проект:** `npx devignerui init` (один раз), затем `npx devignerui add <name>` → `components/ui/<name>.tsx`.
- **Наш `index.html` (ванильный Telegram Mini App):** React-код напрямую не вставлять. Прочитай гайд компонента и перенеси идею, тайминги и поведение на ванильный CSS/JS (анимации через `transform`/`opacity`, уважай `prefers-reduced-motion`).

Гайд по компоненту (props, клавиатура, a11y, edge cases): `curl -s https://ui.devigner.cc/llms/<name>.md`. Это справочный материал.

## Компоненты

| name | Категория | Что это |
|---|---|---|
| tabs | Forms | Пилюли в кольце, мягкая подложка скользит к выбранной |
| flip-button | Primitives | Текст переворачивается на hover, иконка в бейдже выезжает |
| expand-tabs | Navigation | Иконки-плитки, наведённая расширяется и показывает подпись |
| stardust-slider | Forms | Слайдер со светящейся «пылью» и катящимися цифрами |
| badge | Primitives | Бейдж статуса/фильтра со счётчиком и удалением |
| image-generation | Animation | Сетка точек, растворяющаяся в готовое изображение |
| kpi-chart | Data Display | Рисующаяся кривая роста + карточка с катящимися цифрами |
| contact-lens | Animation | Строка контактов, иконка подсвечивает свою часть, клик копирует |
| copy-button | Primitives | Копирует и перепечатывает подпись на «Copied» |
| nav-notch | Navigation | Навбар из засечек с эффектом линзы |
| liquid-glass | Primitives | Стеклянное меню в стиле iOS, пилюля → панель |
| slider | Forms | Заливка от жёлтого к красному, стеклянный бегунок |
| file-upload | Forms | Загрузка файлов «жидкостью», очередь/пауза/повтор |
| goo-switch | Forms | Свитч-капля с хвостом |
| inline-time-edit | Forms | Время 2:30 раскрывается в поля часов/минут |
| date-range-picker | Forms | Календарь с превью диапазона и пресетами |
| chart-card | Data Display | Карточка со столбиками, значение при наведении |
| delete-button | Primitives | Подтверждение → ожидание API → галочка |
| menu-dock | Navigation | Док, разрастающийся во вложенные меню |
| timeline | Data Display | Активный шаг съезжает с линии, линия изгибается |
| magnetic-button | Animation | Тянется к курсору, отпускает с покачиванием |
