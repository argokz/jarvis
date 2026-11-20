/**
 * Жесты плитки устройства: нажатие переключает, протяжка по горизонтали меняет уровень (яркость, громкость,
 * положение), долгое нажатие открывает подробности. Вертикальное движение — это прокрутка страницы, а не жест.
 */
export const DRAG_PX = 10; // с какого сдвига движение считается протяжкой
export const LONG_MS = 500;

/** Уровень 1–100 по положению пальца на плитке: левый край — минимум, правый — максимум. */
export function levelAt(x: number, left: number, width: number): number {
  if (width <= 0) return 1;
  return Math.min(100, Math.max(1, Math.round(((x - left) / width) * 100)));
}

export type Outcome = "tap" | "drag" | "long" | "none";

export class Gesture {
  dragging = false;
  private x0 = 0;
  private y0 = 0;
  private t0 = 0;
  private dead = true; // жест отменён (прокрутка, уже сработало долгое нажатие) или ещё не начат

  down(x: number, y: number, t: number): void {
    [this.x0, this.y0, this.t0, this.dragging, this.dead] = [x, y, t, false, false];
  }

  /** Возвращает true, пока идёт протяжка. canDrag — у устройства есть уровень, который можно менять. */
  move(x: number, y: number, canDrag: boolean): boolean {
    if (this.dead) return false;
    const [dx, dy] = [Math.abs(x - this.x0), Math.abs(y - this.y0)];
    if (!this.dragging) {
      if (dy > DRAG_PX && dy >= dx) this.dead = true; // страницу прокручивают
      else if (dx > DRAG_PX && canDrag) this.dragging = true;
      else if (dx > DRAG_PX * 3) this.dead = true; // увели палец в сторону с плитки без уровня — не нажатие
    }
    return this.dragging;
  }

  /** Палец ещё на месте — можно считать нажатие долгим. */
  get still(): boolean {
    return !this.dead && !this.dragging;
  }

  cancel(): void {
    this.dead = true;
  }

  up(t: number): Outcome {
    if (this.dead) return "none";
    this.dead = true;
    if (this.dragging) return "drag";
    return t - this.t0 >= LONG_MS ? "long" : "tap";
  }
}
