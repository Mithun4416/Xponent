---
name: Restaurant Intelligence Interface
colors:
  surface: '#f9f9ff'
  surface-dim: '#d4dae9'
  surface-bright: '#f9f9ff'
  surface-container-lowest: '#ffffff'
  surface-container-low: '#f0f3ff'
  surface-container: '#e8eefd'
  surface-container-high: '#e3e8f7'
  surface-container-highest: '#dde2f1'
  on-surface: '#161c26'
  on-surface-variant: '#5b403e'
  inverse-surface: '#2b313c'
  inverse-on-surface: '#ecf1ff'
  outline: '#906f6d'
  outline-variant: '#e4bdba'
  surface-tint: '#bb1522'
  primary: '#b81120'
  on-primary: '#ffffff'
  primary-container: '#dc3135'
  on-primary-container: '#fffbff'
  inverse-primary: '#ffb3ae'
  secondary: '#5c5e66'
  on-secondary: '#ffffff'
  secondary-container: '#dfdfe9'
  on-secondary-container: '#61626b'
  tertiary: '#b71423'
  on-tertiary: '#ffffff'
  tertiary-container: '#db3239'
  on-tertiary-container: '#fffbff'
  error: '#ba1a1a'
  on-error: '#ffffff'
  error-container: '#ffdad6'
  on-error-container: '#93000a'
  primary-fixed: '#ffdad7'
  primary-fixed-dim: '#ffb3ae'
  on-primary-fixed: '#410004'
  on-primary-fixed-variant: '#930014'
  secondary-fixed: '#e2e2ec'
  secondary-fixed-dim: '#c5c6d0'
  on-secondary-fixed: '#191b22'
  on-secondary-fixed-variant: '#45464e'
  tertiary-fixed: '#ffdad7'
  tertiary-fixed-dim: '#ffb3ae'
  on-tertiary-fixed: '#410005'
  on-tertiary-fixed-variant: '#930016'
  background: '#f9f9ff'
  on-background: '#161c26'
  surface-variant: '#dde2f1'
typography:
  headline-xl:
    fontFamily: Inter
    fontSize: 28px
    fontWeight: '700'
    lineHeight: 36px
  headline-lg:
    fontFamily: Inter
    fontSize: 22px
    fontWeight: '700'
    lineHeight: 30px
  headline-md:
    fontFamily: Inter
    fontSize: 18px
    fontWeight: '600'
    lineHeight: 26px
  headline-sm:
    fontFamily: Inter
    fontSize: 16px
    fontWeight: '600'
    lineHeight: 24px
  kpi-value:
    fontFamily: Inter
    fontSize: 24px
    fontWeight: '700'
    lineHeight: 32px
    letterSpacing: -0.02em
  body-lg:
    fontFamily: Inter
    fontSize: 15px
    fontWeight: '400'
    lineHeight: 22px
  body-md:
    fontFamily: Inter
    fontSize: 13px
    fontWeight: '400'
    lineHeight: 20px
  body-sm:
    fontFamily: Inter
    fontSize: 12px
    fontWeight: '400'
    lineHeight: 18px
  label-md:
    fontFamily: Inter
    fontSize: 13px
    fontWeight: '500'
    lineHeight: 18px
  label-sm:
    fontFamily: Inter
    fontSize: 11px
    fontWeight: '600'
    lineHeight: 16px
    letterSpacing: 0.02em
rounded:
  sm: 0.25rem
  DEFAULT: 0.5rem
  md: 0.75rem
  lg: 1rem
  xl: 1.5rem
  full: 9999px
spacing:
  gutter: 1.25rem
  gutter-mobile: 0.75rem
  margin: 1.5rem
  margin-mobile: 1rem
  space-xs: 0.25rem
  space-sm: 0.5rem
  space-md: 0.875rem
  space-lg: 1.25rem
  space-xl: 1.75rem
  space-2xl: 2.5rem
---

## Brand & Style

This design system establishes a high-performance, executive analytics interface tailored for restaurant intelligence, revenue operations, and natural language query systems. The visual architecture bridges modern AI tooling with mission-critical enterprise operations: calm, legible, and structured during continuous daily usage, yet decisively responsive when highlighting key metrics and computational actions.

The aesthetic blends **Modern Corporate** clarity with subtle **Tactile & Ambient** nuances:
- **Foundational Canvas**: A high-efficiency workspace pairing an anchored dark charcoal sidebar with an airy, lavender-slate canvas that minimizes eye fatigue during long operational shifts.
- **Accents & Telemetry**: Warm scarlet and coral-red active elements (`#E5383B`) evoke urgency, warmth, and hospitality energy without visual clutter.
- **Atmosphere**: Elevated white surfaces feature generous 16px soft radii, delicate borders, and low-opacity ambient shadows that allow complex analytical dashboards, status badges, and natural language prompts to coexist harmoniously.

## Colors

The palette balances high-contrast navigation with a calm, data-focused workspace:

- **Primary (`#E5383B`)**: The primary brand accent and decisive interaction color. Applied to active states, prominent action triggers (such as query executions), interactive link elements, and priority metric icon containers.
- **Secondary (`#16181F`)**: Deep slate charcoal forming the persistent vertical command sidebar, primary typographic titles, and grounding elements across the layout.
- **Tertiary (`#FF4D4F`)**: Glowing coral-red, utilized for subtle active gradients, focus indicators, and warning or status pills requiring immediate operator attention.
- **Neutral Palette**:
  - **Canvas Tint (`#F0F2F7`)**: Soft cool grayish-blue/lavender background that defines depth against pure white cards.
  - **Surface Default (`#FFFFFF`)**: Pure crisp white for metric panels, input cards, and interactive widgets.
  - **Surface Subdued (`#F8F9FC`)**: Secondary container background for nested items, code summaries, and metadata clusters.
  - **Border Soft (`#E3E6EF`)**: Whisper-weight neutral stroke defining structural separation.
  - **Text Primary (`#111318`)**: Deep charcoal-black for headlines, KPI readouts, and primary metrics.
  - **Text Secondary (`#6C727F`)**: Balanced slate-gray for contextual descriptions, timestamps, and column labels.
  - **Status Accents**: Soft pink tint (`#FDE8E9`) for active badge pills and primary icon plates; soft emerald (`#E6F4EA` / `#137333`) for real-time freshness telemetry.

## Typography

This design system uses **Inter** across all typographic hierarchies to provide programmatic clarity and tabular alignment across high-volume restaurant data.

- **KPI Numerical Display**: Primary numerical values (`kpi-value`) utilize tight tracking (`-0.02em`) with proportional and tabular numeric figures enabled (`font-variant-numeric: tabular-nums;`), ensuring currency formats and counts stack seamlessly.
- **Editorial Directives**: Section greetings and AI interaction headers use strong weights (`600`–`700`) paired with relaxed secondary instructions (`body-md`) to clearly separate system status from operational tasks.
- **Labels & Micro-data**: Metadata tags, scope locks, and execution timestamps employ semi-bold styling (`label-sm`) with uppercase or title casing for instantaneous legibility at miniature sizes.

## Layout & Spacing

The interface is structured upon an asymmetric fluid architecture:
- **Navigation Rail / Sidebar**: Fixed-width docking column (`240px` on desktop) styled in dark charcoal, isolating navigation controls from data exploration surfaces.
- **Main Canvas**: A fluid grid using an 8pt base modular spacing system. Internal dashboard views rely on a 12-column layout with standard `1.25rem` (20px) gutters.
- **Card Padding**: Primary content surfaces adhere to an internal padding of `1.25rem` (`space-lg`), while compact KPI summary tiles employ `1rem` to preserve vertical density.
- **Responsive Adaptations**:
  - *Desktop (>1200px)*: Multi-column arrangement featuring 3-to-4 horizontal metric widgets, two-column natural-language and question stream areas, and dedicated side widgets.
  - *Tablet (768px - 1199px)*: Sidebar collapses into an icon-rail (`68px`); metric grid reflows to 2 columns; complex analytics cards stretch full-width.
  - *Mobile (<767px)*: Sidebar retreats to an off-canvas drawer; metric cards and question cards stack into a single column with compacted gutters (`gutter-mobile: 0.75rem`).

## Elevation & Depth

Visual depth is achieved through an interplay of planar contrasts, soft ambient shadows, and luminous active accents rather than hard structural lines:

- **Canvas to Surface**: The background sits at base elevation `#F0F2F7`. Floating metric cards and widgets occupy Elevation Level 1, utilizing a soft ambient drop shadow:
  `box-shadow: 0 4px 20px -2px rgba(22, 24, 31, 0.05), 0 1px 3px rgba(22, 24, 31, 0.03);`
  accompanied by a 1px border stroke: `rgba(227, 230, 239, 0.8)`.
- **Active Floating Elements**: Interactive components such as the active navigation pill and primary execution buttons leverage an elevated coral glow:
  `box-shadow: 0 4px 14px 0 rgba(229, 56, 59, 0.35);`
- **Subdued Inset Containers**: Nested metadata panels, example question pill containers, and input wells utilize a flat inset or tonal shift to `#F8F9FC` with a hairline internal outline (`#E3E6EF`), maintaining hierarchy without nested drop shadows.

## Shapes

The design system establishes a welcoming, modern feel through rounded forms (Level 2 roundedness):

- **Cards & Primary Surfaces**: Uniform `1rem` (16px) corner radius across all dashboard cards, result displays, and prompt containers.
- **Interactive Buttons & Badges**: Small action buttons, search bars, and filter selectors feature `0.5rem` (8px) corners, while quick-filter suggestion prompts and scope badges adopt smooth pill capsules (`9999px`).
- **Sidebar Selection Indicator**: The active sidebar pill combines an integrated `0.75rem` (12px) curve with inset edge alignment to provide clear navigational feedback.
- **Icon Plates**: Metric icon backdrops use a squircle format with `0.75rem` (12px) border radius, giving financial, order, and telemetry icons an organized, app-like presence.

## Components

### Buttons & Action Controls
- **Primary Execution Button**: Solid fill with a gradient transition from `#E5383B` to `#FF4343`. White text, semi-bold weight, `8px` corner radius, and subtle coral drop shadow. Includes an inline leading run icon.
- **Ghost & Secondary Buttons**: Clean white or transparent background with a neutral border (`#DDE1EC`), charcoal text (`#16181F`), and hover fill transition to `#F8F9FC`.
- **Sidebar Navigation Links**: Default state in muted lavender-gray (`#9CA3AF`) on `#16181F`. Active state transforms into a solid scarlet-coral pill (`#E5383B`) with white text and an ambient red glow.

### Metric KPI Cards
- Pure white container with `16px` border-radius and ambient shadow.
- Top row aligns an icon badge (tinted background `#FDE8E9` for primary metrics, `#EDF2FE` for secondary operations) alongside the KPI title in slate neutral.
- Main value styled in `24px` bold with currency symbols and numeric figures.
- Bottom sub-label indicates duration context ("Last 30 days") in muted typography.

### Input Fields & Prompt Bar
- **Natural Language Search Input**: Expansive text area or unified single-line search bar with a soft border (`#DDE1EC`), white surface, `8px` corner radius, and slate placeholder styling.
- Accompanied by integrated right-aligned execution action (`Run`) and interactive example pills directly beneath for guided user flows.

### Badges & Filter Chips
- **Scope Locked & Security Badges**: Soft pink tint (`#FDE8E9`) container, sharp crimson text (`#E5383B`), subtle lock icon, and pill-shaped radius.
- **Telemetry & Status Badges**: Light neutral or soft green container with an embedded pulse indicator dot signifying live data freshness ("Updated 0.1 seconds ago").
- **Interactive Suggestion Chips**: Pill shapes (`#F8F9FC` surface, `#E3E6EF` border) with subtle hover scale and text transition to dark slate.

### Data Tables & Analytics Widgets
- Tables feature borderless, airy rows separated by single-pixel rules (`#F0F2F7`).
- Column headers styled with uppercase `label-sm` weights in neutral gray.
- Tabular figures utilized across all numerical and financial metrics to preserve column alignment.