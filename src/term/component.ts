/**
 * What a component is, and how containers put them together.
 *
 * A component is a pure function from a width to a list of lines: every line at
 * most `width` cells wide, every line ending in a full reset. A container
 * renders its children and concatenates the results. A child that has not
 * been invalidated hands back the lines it produced last time, so a spinner
 * ticking beside a long transcript costs one child's render and a join.
 */

export interface Component {
  render(width: number): string[];
}

/** A component that can take a keystroke; returns whether it consumed it. */
export interface Interactive {
  handleInput(key: string, data: string): boolean;
}

/** Base class supplying the dirty-tracking half of the contract. */
export abstract class Widget implements Component {
  private cache: string[] = [];
  private cacheWidth = -1;
  protected isDirty = true;

  abstract draw(width: number): string[];

  render(width: number): string[] {
    if (this.isDirty || width !== this.cacheWidth) {
      this.cache = this.draw(width);
      this.cacheWidth = width;
      this.isDirty = false;
    }
    return this.cache;
  }

  invalidate(): void {
    this.isDirty = true;
  }

  get dirty(): boolean {
    return this.isDirty;
  }
}

/** Children stacked vertically. The only layout there is. */
export class Container extends Widget {
  children: Component[];

  constructor(...children: Component[]) {
    super();
    this.children = children;
  }

  add<T extends Component>(child: T): T {
    this.children.push(child);
    this.invalidate();
    return child;
  }

  extend(children: Component[]): void {
    this.children.push(...children);
    this.invalidate();
  }

  clear(): void {
    this.children = [];
    this.invalidate();
  }

  remove(child: Component): void {
    const index = this.children.indexOf(child);
    if (index >= 0) {
      this.children.splice(index, 1);
      this.invalidate();
    }
  }

  /** Concatenate the children, asking each: a clean child returns its cache,
   * so a frame costs what actually changed. */
  override render(width: number): string[] {
    const lines: string[] = [];
    for (const child of this.children) lines.push(...child.render(width));
    this.isDirty = false;
    return lines;
  }

  draw(width: number): string[] {
    return this.render(width);
  }

  /** Offer the key to each child until one takes it. */
  handleInput(key: string, data: string): boolean {
    for (const child of this.children) {
      const handler = (child as Partial<Interactive>).handleInput;
      if (handler?.call(child, key, data)) return true;
    }
    return false;
  }
}
