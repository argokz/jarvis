/**
 * Токены оформления: один JSON — источник цветов для всего клиента (и для будущего экспорта в Dart).
 * Тема тёмная по умолчанию, без чистого чёрного; цвета закреплены за смыслом: свет — янтарный,
 * холод и тепло — голубой и розовый, «в порядке» — зелёный, авария — красный, ассистент и медиа — индиго.
 */
import tokens from "../tokens.json";

export { tokens };

/** Имена и значения CSS-переменных: --bg, --surface, …, --radius, --motion. */
export function cssVars(t: typeof tokens = tokens): Record<string, string> {
  const out: Record<string, string> = { "--radius": t.radius, "--motion": t.motion };
  for (const [name, value] of Object.entries(t.color)) out[`--${name}`] = value;
  return out;
}

export function applyTokens(root: { style: { setProperty(name: string, value: string): void } }): void {
  for (const [name, value] of Object.entries(cssVars())) root.style.setProperty(name, value);
}

/** Контраст двух цветов #RRGGBB по WCAG 2: от 1 до 21. */
export function contrast(a: string, b: string): number {
  const luminance = (hex: string) => {
    const [r, g, bl] = [1, 3, 5].map((i) => {
      const c = parseInt(hex.slice(i, i + 2), 16) / 255;
      return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
    });
    return 0.2126 * r + 0.7152 * g + 0.0722 * bl;
  };
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}
