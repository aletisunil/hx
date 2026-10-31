/**
 * Protocol Buffers wire format, by hand.
 *
 * The Devin route speaks protobuf, and only a few dozen fields of it: a
 * varint, a length prefix and two fixed-width floats. Encoding is
 * field-by-field with the `put*` helpers on `Writer`, which skip proto3
 * default values exactly as a generated encoder does. Decoding is
 * schema-less: `parse` turns a message into its fields by number, and the
 * `Fields` accessors read them as the caller's schema says they are.
 */

export const VARINT = 0;
export const FIXED64 = 1;
export const LEN = 2;
export const FIXED32 = 5;

/** Bytes that are not a well-formed protobuf message. */
export class WireError extends Error {
  override name = "WireError";
}

function varint(given: number | bigint): Buffer {
  // Negative int32/int64 are sign-extended to ten bytes on the wire.
  let value = BigInt(given);
  if (value < 0n) value += 1n << 64n;
  const out: number[] = [];
  for (;;) {
    const byte = Number(value & 0x7fn);
    value >>= 7n;
    if (value) out.push(byte | 0x80);
    else {
      out.push(byte);
      return Buffer.from(out);
    }
  }
}

const utf8 = (text: string) => Buffer.from(text, "utf8");

/** Accumulates one message. Every `put*` omits a proto3 default. */
export class Writer {
  private readonly parts: Buffer[] = [];

  private tag(number: number, wireType: number): void {
    this.parts.push(varint((number << 3) | wireType));
  }

  putUint(number: number, value: number): void {
    if (!value) return;
    this.tag(number, VARINT);
    this.parts.push(varint(value));
  }

  putBool(number: number, value: boolean): void {
    this.putUint(number, value ? 1 : 0);
  }

  putDouble(number: number, value: number): void {
    if (!value) return;
    this.tag(number, FIXED64);
    const raw = Buffer.alloc(8);
    raw.writeDoubleLE(value);
    this.parts.push(raw);
  }

  putStr(number: number, value: string): void {
    if (value) this.putBytes(number, utf8(value));
  }

  putBytes(number: number, value: Buffer): void {
    if (!value.length) return;
    this.tag(number, LEN);
    this.parts.push(varint(value.length), value);
  }

  /** A present sub-message is written even when empty: presence is the point. */
  putMessage(number: number, value: Writer | Buffer | null): void {
    if (value === null) return;
    const raw = value instanceof Writer ? value.finish() : value;
    this.tag(number, LEN);
    this.parts.push(varint(raw.length), raw);
  }

  /** Repeated strings keep empty entries; only singular fields elide. */
  putStrs(number: number, values: Iterable<string>): void {
    for (const value of values) {
      const raw = utf8(value);
      this.tag(number, LEN);
      this.parts.push(varint(raw.length), raw);
    }
  }

  putPackedUints(number: number, values: Iterable<number>): void {
    this.putBytes(number, Buffer.concat([...values].map((value) => varint(value))));
  }

  finish(): Buffer {
    return Buffer.concat(this.parts);
  }
}

type FieldValue = bigint | Buffer;

/** Decoded text, undecodable bytes shown as U+FFFD. */
const decode = (raw: Buffer) => new TextDecoder("utf-8").decode(raw);

/** A decoded message: raw values by field number, in wire order. */
export class Fields {
  constructor(private readonly fields: Map<number, [number, FieldValue][]>) {}

  /** Proto3: for a singular field, the last occurrence wins. */
  private last(number: number): [number, FieldValue] | null {
    return this.fields.get(number)?.at(-1) ?? null;
  }

  has(number: number): boolean {
    return this.fields.has(number);
  }

  private big(number: number): bigint {
    const entry = this.last(number);
    return entry !== null && entry[0] === VARINT ? (entry[1] as bigint) : 0n;
  }

  uint(number: number): number {
    return Number(this.big(number));
  }

  int32(number: number): number {
    return Number(BigInt.asIntN(32, this.big(number)));
  }

  flag(number: number): boolean {
    return this.big(number) !== 0n;
  }

  f32(number: number): number {
    const entry = this.last(number);
    return entry !== null && entry[0] === FIXED32 ? (entry[1] as Buffer).readFloatLE() : 0;
  }

  f64(number: number): number {
    const entry = this.last(number);
    return entry !== null && entry[0] === FIXED64 ? (entry[1] as Buffer).readDoubleLE() : 0;
  }

  raw(number: number): Buffer {
    const entry = this.last(number);
    return entry !== null && entry[0] === LEN ? (entry[1] as Buffer) : Buffer.alloc(0);
  }

  text(number: number): string {
    return decode(this.raw(number));
  }

  message(number: number): Fields | null {
    const entry = this.last(number);
    return entry !== null && entry[0] === LEN ? parse(entry[1] as Buffer) : null;
  }

  messages(number: number): Fields[] {
    return (this.fields.get(number) ?? [])
      .filter(([type]) => type === LEN)
      .map(([, value]) => parse(value as Buffer));
  }

  texts(number: number): string[] {
    return (this.fields.get(number) ?? [])
      .filter(([type]) => type === LEN)
      .map(([, value]) => decode(value as Buffer));
  }
}

function readVarint(data: Buffer, start: number): [bigint, number] {
  let result = 0n;
  let shift = 0n;
  let pos = start;
  for (;;) {
    if (pos >= data.length) throw new WireError("truncated varint");
    const byte = data[pos] as number;
    pos++;
    result |= BigInt(byte & 0x7f) << shift;
    if (!(byte & 0x80)) return [result, pos];
    shift += 7n;
    if (shift >= 70n) throw new WireError("varint too long");
  }
}

/**
 * Split a message into its fields. Throws `WireError` on truncation or a wire
 * type protobuf does not define; a group is refused rather than skipped.
 */
export function parse(data: Buffer): Fields {
  const fields = new Map<number, [number, FieldValue][]>();
  let pos = 0;
  const end = data.length;
  while (pos < end) {
    let key: bigint;
    [key, pos] = readVarint(data, pos);
    const number = Number(key >> 3n);
    const wireType = Number(key & 7n);
    if (number === 0) throw new WireError("field number 0");
    let value: FieldValue;
    if (wireType === VARINT) [value, pos] = readVarint(data, pos);
    else if (wireType === FIXED64) {
      if (pos + 8 > end) throw new WireError("truncated fixed64");
      value = data.subarray(pos, pos + 8);
      pos += 8;
    } else if (wireType === LEN) {
      let length: bigint;
      [length, pos] = readVarint(data, pos);
      if (pos + Number(length) > end) throw new WireError("truncated length-delimited field");
      value = data.subarray(pos, pos + Number(length));
      pos += Number(length);
    } else if (wireType === FIXED32) {
      if (pos + 4 > end) throw new WireError("truncated fixed32");
      value = data.subarray(pos, pos + 4);
      pos += 4;
    } else throw new WireError(`unsupported wire type ${wireType}`);
    const list = fields.get(number);
    if (list === undefined) fields.set(number, [[wireType, value]]);
    else list.push([wireType, value]);
  }
  return new Fields(fields);
}
