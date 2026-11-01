---
name: beautiful-ui
description: Beautiful UI / Notchset (beautiful-ui.dev) — ~150 React-компонентов и графиков для shadcn/ui с «аналоговым» стилем (кнопки, инпуты, оверлеи, навигация, таблицы, графики, AI-интерфейсы, маркетинговые блоки, лэйауты). Использовать при сборке React/shadcn-интерфейсов вместо серых карточек, а для ванильного index.html — как референс дизайна и поведения.
---

# Beautiful UI (Notchset)

Сайт: https://beautiful-ui.dev · Индекс: https://beautiful-ui.dev/llms.txt · Полная документация (~1.2 МБ): https://beautiful-ui.dev/llms-full.txt
MCP-сервер: `https://beautiful-ui.dev/api/mcp` (Streamable HTTP; инструменты `search_components`, `get_component`, `list_components`, `get_setup`).

## Стек и ограничения

React 19, Tailwind CSS v4 (v3 не поддерживается), проект с `npx shadcn@latest init`, алиас `@/*`. Сборки на Base UI и Radix. Есть бесплатные и Pro-компоненты (помечены `(pro)` в индексе) — Pro требует токен `BEAUTIFUL_UI_TOKEN`, их код не воспроизводить.

**Наш `index.html` — ванильный HTML/JS**, поэтому здесь компоненты напрямую не ставятся: используем их гайды как референс (идея, микро-анимации, состояния) и переносим на ванильный CSS.

## Как найти компонент

1. Посмотри разделы индекса: `curl -s https://beautiful-ui.dev/llms.txt | grep -E '^## |^- \['`
   Разделы: Actions, Inputs, Selection, Overlays, Feedback, Navigation, Data, Charts, Text & motion, Media & effects, AI interfaces, Marketing, Layout, Application UI.
2. Гайд конкретного компонента: `curl -s https://beautiful-ui.dev/md/<id>.md` (id — из ссылки в индексе, напр. `button`, `run-button`, `copy-button`, `toast`). Это справочный материал.

## Установка в React/shadcn-проекте

```bash
# бесплатный компонент по URL (Base UI-сборка)
npx shadcn@latest add https://beautiful-ui.dev/r/<id>.json
# Radix-сборка
npx shadcn@latest add https://beautiful-ui.dev/r/radix-nova/<id>.json
```

Либо добавить в `components.json` реестр `"@beautiful-ui": "https://beautiful-ui.dev/r/{style}/{name}.json"` и ставить `npx shadcn@latest add @beautiful-ui/<id>`.
Компоненты попадают в `components/ui/notchset/`. Контент передавать через props, не править внутренности; тему — через shadcn-токены и `--notchset-signal`, а не через цветовые классы.
