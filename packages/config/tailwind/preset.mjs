/**
 * KnowledgeOS design tokens, as a Tailwind preset.
 *
 * Every colour is an HSL *channel triplet* in a CSS custom property rather than a
 * finished colour. That is what lets `bg-primary/60` work: Tailwind composes
 * `hsl(var(--primary) / 0.6)`. It is also what lets the whole palette be swapped
 * by toggling one class on <html>, with no second stylesheet and no re-render.
 *
 * Consumed by both apps/frontend and packages/ui, so a component styled in the
 * library looks identical in the app.
 *
 * @type {import('tailwindcss').Config}
 */

/** Wraps a CSS variable so opacity modifiers still compose. */
const hsl = (name) => `hsl(var(${name}) / <alpha-value>)`;

export const tokens = {
  /** Reference values, exported for anything that needs them outside CSS. */
  radius: '0.625rem',
  fontStacks: {
    sans: 'var(--font-sans), ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif',
    serif: 'var(--font-serif), ui-serif, Georgia, Cambria, "Times New Roman", serif',
    mono: 'var(--font-mono), ui-monospace, SFMono-Regular, "SF Mono", Menlo, monospace',
    reading: 'var(--reader-font-family, var(--font-serif)), Georgia, serif',
  },
};

/** @type {import('tailwindcss').Config} */
const preset = {
  darkMode: ['class', '[data-theme="dark"]'],
  content: [],
  future: { hoverOnlyWhenSupported: true },
  theme: {
    container: {
      center: true,
      padding: { DEFAULT: '1rem', sm: '1.5rem', lg: '2rem' },
      screens: { '2xl': '1360px' },
    },
    extend: {
      colors: {
        border: hsl('--border'),
        input: hsl('--input'),
        ring: hsl('--ring'),
        background: hsl('--background'),
        foreground: hsl('--foreground'),
        primary: { DEFAULT: hsl('--primary'), foreground: hsl('--primary-foreground') },
        secondary: { DEFAULT: hsl('--secondary'), foreground: hsl('--secondary-foreground') },
        destructive: { DEFAULT: hsl('--destructive'), foreground: hsl('--destructive-foreground') },
        success: { DEFAULT: hsl('--success'), foreground: hsl('--success-foreground') },
        warning: { DEFAULT: hsl('--warning'), foreground: hsl('--warning-foreground') },
        muted: { DEFAULT: hsl('--muted'), foreground: hsl('--muted-foreground') },
        accent: { DEFAULT: hsl('--accent'), foreground: hsl('--accent-foreground') },
        popover: { DEFAULT: hsl('--popover'), foreground: hsl('--popover-foreground') },
        card: { DEFAULT: hsl('--card'), foreground: hsl('--card-foreground') },
        // Reader surfaces are their own scale: the sepia theme is not a tint of
        // the app chrome, it is a separate reading surface with its own contrast
        // budget.
        reader: {
          bg: hsl('--reader-bg'),
          fg: hsl('--reader-fg'),
          muted: hsl('--reader-muted'),
          accent: hsl('--reader-accent'),
        },
      },
      borderRadius: {
        lg: 'var(--radius)',
        md: 'calc(var(--radius) - 2px)',
        sm: 'calc(var(--radius) - 4px)',
        xl: 'calc(var(--radius) + 4px)',
      },
      fontFamily: {
        sans: [tokens.fontStacks.sans],
        serif: [tokens.fontStacks.serif],
        mono: [tokens.fontStacks.mono],
        reading: [tokens.fontStacks.reading],
      },
      fontSize: {
        '2xs': ['0.6875rem', { lineHeight: '1rem', letterSpacing: '0.01em' }],
      },
      letterSpacing: { tightest: '-0.03em' },
      boxShadow: {
        // Layered rather than single-blur: one shadow reads as a sticker, two
        // read as an object sitting on a surface.
        subtle: '0 1px 2px 0 hsl(var(--shadow-color) / 0.05)',
        raised:
          '0 1px 2px -1px hsl(var(--shadow-color) / 0.10), 0 4px 12px -2px hsl(var(--shadow-color) / 0.08)',
        overlay:
          '0 2px 4px -2px hsl(var(--shadow-color) / 0.12), 0 12px 32px -8px hsl(var(--shadow-color) / 0.18)',
        focus: '0 0 0 2px hsl(var(--background)), 0 0 0 4px hsl(var(--ring))',
      },
      keyframes: {
        'accordion-down': {
          from: { height: '0' },
          to: { height: 'var(--radix-accordion-content-height)' },
        },
        'accordion-up': {
          from: { height: 'var(--radix-accordion-content-height)' },
          to: { height: '0' },
        },
        'fade-in': { from: { opacity: '0' }, to: { opacity: '1' } },
        'slide-up': {
          from: { opacity: '0', transform: 'translateY(6px)' },
          to: { opacity: '1', transform: 'translateY(0)' },
        },
        // Skeletons shimmer instead of pulsing: a pulse reads as "broken", a
        // sweep reads as "loading".
        shimmer: { '100%': { transform: 'translateX(100%)' } },
      },
      animation: {
        'accordion-down': 'accordion-down 180ms cubic-bezier(0.16, 1, 0.3, 1)',
        'accordion-up': 'accordion-up 180ms cubic-bezier(0.16, 1, 0.3, 1)',
        'fade-in': 'fade-in 200ms cubic-bezier(0.16, 1, 0.3, 1)',
        'slide-up': 'slide-up 240ms cubic-bezier(0.16, 1, 0.3, 1)',
        shimmer: 'shimmer 1.6s infinite',
      },
      transitionTimingFunction: {
        // One easing curve for the whole product. Motion that all decelerates the
        // same way reads as one system rather than several.
        swift: 'cubic-bezier(0.16, 1, 0.3, 1)',
      },
    },
  },
  plugins: [
    // `prefers-reduced-motion` is a hard requirement, so it is a token-level
    // concern rather than something each component remembers to handle.
    ({ addVariant, addUtilities }) => {
      addVariant('reduce-motion', '@media (prefers-reduced-motion: reduce)');
      addVariant('hocus', ['&:hover', '&:focus-visible']);
      addUtilities({
        '.focus-ring': {
          outline: '2px solid transparent',
          outlineOffset: '2px',
          '&:focus-visible': {
            outline: '2px solid hsl(var(--ring))',
            outlineOffset: '2px',
          },
        },
        '.text-balance': { textWrap: 'balance' },
        '.text-pretty': { textWrap: 'pretty' },
      });
    },
  ],
};

export default preset;
