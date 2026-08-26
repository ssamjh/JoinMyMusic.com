/**
 * Class lists shared by more than one component. Kept here rather than as
 * `@layer components` rules so the Tailwind utilities stay visible in the
 * markup, but without repeating a 12-utility button recipe four times.
 */

/** Modal footer buttons. Preflight already zeroes the border, so `btn` sets none. */
export const btn =
  "flex h-12 cursor-pointer items-center justify-center gap-2 rounded-[14px] text-[15px] font-semibold transition-[transform,background-color,opacity] duration-200 disabled:pointer-events-none disabled:cursor-not-allowed disabled:opacity-50";

export const btnGhost = `${btn} flex-1 border border-line bg-transparent text-fg hover:bg-elev`;

export const btnAccent = `${btn} flex-[1.4] bg-accent text-white shadow-[0_10px_28px_-8px_var(--accent-glow)] hover:-translate-y-px active:scale-[0.97]`;

export const btnDanger = `${btn} flex-[1.4] bg-danger text-white shadow-[0_10px_28px_-8px_rgba(255,77,79,0.55)] hover:-translate-y-px active:scale-[0.97]`;

/** The row of buttons across the bottom of a modal card. */
export const modalActions = "flex gap-2.5 px-6 pt-5 pb-6";

/** Turnstile widgets are centred under whatever they gate. */
export const turnstileWrap = "mt-4 flex justify-center";
