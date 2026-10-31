/**
 * Turning image bytes into something every route will accept.
 *
 * An image reaches the model from three places - pasted into the prompt, read
 * from disk by the Read tool, or returned by an MCP server - and whatever it
 * was, it leaves here as an `ImageBlock` that no provider will reject:
 *
 * - **A format every route takes.** PNG, JPEG, GIF and WebP pass through; a BMP
 *   or TIFF (what the macOS clipboard hands over) is re-encoded as PNG.
 * - **Bounded dimensions.** The long edge is capped at `MAX_EDGE`.
 * - **Bounded bytes.** `MAX_BYTES` keeps the base64 under Anthropic's 5 MB
 *   per-image limit, the tightest of the routes.
 *
 * Nothing is re-encoded that does not have to be. An image already within every
 * limit is sent byte for byte, so a screenshot of small text keeps every pixel.
 */

import { readFileSync, statSync } from "node:fs";
import type { Sharp } from "sharp";
import { basename, expandUser, isFile } from "../pathlib.ts";
import { formatFixed, pyRound, strip } from "../py.ts";
import { split } from "../shlex.ts";
import {
  type ContentBlock,
  type ImageBlock,
  imageBlock,
  type Message,
  textBlock,
} from "./messages.ts";

/** What every route accepts as it stands. */
export const MEDIA_TYPES = new Set(["image/png", "image/jpeg", "image/gif", "image/webp"]);

/** Longest side sent, in pixels. */
export const MAX_EDGE = 2000;

/** Largest encoded image sent. Its base64 is 5 MB, Anthropic's per-image cap. */
export const MAX_BYTES = 3_750_000;

/** Largest file even opened. A decoder handed a gigabyte of TIFF is how a paste
 * freezes the session. */
export const MAX_SOURCE_BYTES = 64 * 1024 * 1024;

/** Largest image decoded, refused as an error the user is shown. */
export const MAX_SOURCE_PIXELS = 100_000_000;

/** Longest paste still checked for dragged-in paths. */
export const MAX_PASTE_CHARS = 16_384;

/** Extensions treated as images when a path is pasted or read. */
export const IMAGE_SUFFIXES = new Set([
  ".png",
  ".jpg",
  ".jpeg",
  ".gif",
  ".webp",
  ".bmp",
  ".tif",
  ".tiff",
]);

/** Anthropic's published estimate. Close enough for a context gauge. */
const TOKENS_PER_PIXEL = 1 / 750;

/** What Anthropic scales an image down to before counting it. */
const ESTIMATE_EDGE = 1568;
const ESTIMATE_PIXELS = 1_150_000;

const FORMAT_MEDIA_TYPES: Record<string, string> = {
  png: "image/png",
  jpeg: "image/jpeg",
  gif: "image/gif",
  webp: "image/webp",
};

const JPEG_QUALITY = 85;

/** The bytes are not an image HX can send. The message says why, for the user. */
export class ImageError extends Error {
  override name = "ImageError";
}

type SharpFactory = typeof import("sharp").default;
let sharpModule: SharpFactory | null = null;

/** Loaded on first use: the native library costs startup time a text-only session never needs back. */
async function loadSharp(): Promise<SharpFactory> {
  sharpModule ??= (await import("sharp")).default;
  return sharpModule;
}

/** Normalise encoded image bytes into a block every route accepts. */
export async function loadImage(data: Buffer, label = ""): Promise<ImageBlock> {
  const sharp = await loadSharp();
  if (data.length === 0) throw new ImageError("the image is empty");
  if (data.length > MAX_SOURCE_BYTES) {
    throw new ImageError(
      `the image is ${size(data.length)}, over the ${size(MAX_SOURCE_BYTES)} limit`,
    );
  }

  let metadata: Awaited<ReturnType<Sharp["metadata"]>>;
  try {
    metadata = await sharp(data, { limitInputPixels: false }).metadata();
  } catch {
    throw new ImageError("not a readable image");
  }
  const width = metadata.width ?? 0;
  const height = metadata.height ?? 0;
  if (width * height > MAX_SOURCE_PIXELS) {
    throw new ImageError(`the image is ${width}x${height}, too large to decode`);
  }
  try {
    // Decoded in full, so a truncated file is refused here and not by the route.
    await sharp(data, { limitInputPixels: MAX_SOURCE_PIXELS, failOn: "truncated" })
      .raw()
      .toBuffer();
  } catch (error) {
    throw new ImageError(`not a readable image (${(error as Error).message})`);
  }

  const mediaType = FORMAT_MEDIA_TYPES[metadata.format ?? ""];
  const rotated = (metadata.orientation ?? 1) !== 1;
  if (
    mediaType !== undefined &&
    !rotated &&
    Math.max(width, height) <= MAX_EDGE &&
    data.length <= MAX_BYTES
  ) {
    return imageBlock(mediaType, data.toString("base64"), { width, height, label });
  }

  // Everything past here re-encodes. An animated GIF keeps its first frame: no
  // route plays one back, and most would show only that frame anyway.
  try {
    return await reencode(sharp, data, label);
  } catch (error) {
    throw new ImageError(`could not re-encode the image (${(error as Error).message})`);
  }
}

/** Read and normalise an image file. */
export async function loadImageFile(path: string, label?: string): Promise<ImageBlock> {
  let bytes: number;
  try {
    bytes = statSync(path).size;
  } catch (error) {
    throw new ImageError(`cannot read ${path}: ${strerror(error)}`);
  }
  if (bytes > MAX_SOURCE_BYTES) {
    throw new ImageError(
      `${basename(path)} is ${size(bytes)}, over the ${size(MAX_SOURCE_BYTES)} limit`,
    );
  }
  let data: Buffer;
  try {
    data = readFileSync(path);
  } catch (error) {
    throw new ImageError(`cannot read ${path}: ${strerror(error)}`);
  }
  return loadImage(data, label ?? basename(path));
}

/** The OS's own words for a failed file operation: `No such file or directory`. */
function strerror(error: unknown): string {
  const code = (error as NodeJS.ErrnoException).code;
  const known: Record<string, string> = {
    ENOENT: "No such file or directory",
    EACCES: "Permission denied",
    EISDIR: "Is a directory",
    ENOTDIR: "Not a directory",
    ELOOP: "Too many levels of symbolic links",
  };
  return (code && known[code]) || (error as Error).message;
}

export function isImagePath(path: string): boolean {
  const name = basename(path);
  const dot = name.lastIndexOf(".");
  return dot > 0 && IMAGE_SUFFIXES.has(name.slice(dot).toLowerCase());
}

/** Whether a file's first bytes are an image signature the decoder reads. */
export function sniffImage(head: Buffer): boolean {
  const starts = (signature: number[]) => signature.every((byte, index) => head[index] === byte);
  return (
    starts([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]) ||
    starts([0xff, 0xd8, 0xff]) ||
    head.subarray(0, 6).toString("latin1") === "GIF87a" ||
    head.subarray(0, 6).toString("latin1") === "GIF89a" ||
    starts([0x49, 0x49, 0x2a, 0x00]) ||
    starts([0x4d, 0x4d, 0x00, 0x2a]) ||
    (head.subarray(0, 4).toString("latin1") === "RIFF" &&
      head.subarray(8, 12).toString("latin1") === "WEBP")
  );
}

/** How a dragged-in path begins: absolute POSIX, home, `file://`, a Windows
 * drive or UNC share - optionally quoted. */
const PATH_START = /^["']?(?:[/~]|file:\/\/|[A-Za-z]:[\\/]|\\\\)/;

/**
 * The image files a paste names, when naming them is all it does.
 *
 * Dragging a file into a terminal pastes its path - quoted, or with spaces
 * backslash-escaped - and `file://` URIs arrive from some Linux desktops. Any
 * of those, alone or several at once, is read as "attach these". Anything else
 * is `null`, and the paste goes in as the text it was.
 */
export function pastedImagePaths(text: string): string[] | null {
  const stripped = strip(text);
  if (!stripped || [...stripped].length > MAX_PASTE_CHARS || !PATH_START.test(stripped))
    return null;
  let tokens: string[];
  try {
    tokens = split(stripped);
  } catch {
    return null;
  }
  if (tokens.length === 0) return null;
  const paths: string[] = [];
  for (let token of tokens) {
    if (token.startsWith("file://")) {
      try {
        token = decodeURIComponent(new URL(token).pathname);
      } catch {
        return null;
      }
    }
    const path = expandUser(token);
    if (!path.startsWith("/")) return null;
    if (!isImagePath(path) || !isFile(path)) return null;
    paths.push(path);
  }
  return paths;
}

/**
 * Roughly what one image costs in context, for the gauge and compaction. An
 * image whose size was never recorded is charged as a full-size one: guessing
 * low is how compaction fires one turn too late.
 */
export function estimateTokens(image: ImageBlock): number {
  const { width, height } = image;
  if (width <= 0 || height <= 0) return Math.ceil(ESTIMATE_PIXELS * TOKENS_PER_PIXEL);
  const scale = Math.min(
    1,
    ESTIMATE_EDGE / Math.max(width, height),
    Math.sqrt(ESTIMATE_PIXELS / (width * height)),
  );
  return Math.ceil(width * height * scale * scale * TOKENS_PER_PIXEL);
}

/** `screenshot.png · 1440x900 · 312 kB` - how an image is named on screen. */
export function describe(image: ImageBlock): string {
  const parts = [image.label || "image"];
  if (image.source && image.source !== image.label) parts.push(image.source);
  if (image.width && image.height) parts.push(`${image.width}x${image.height}`);
  parts.push(size(Math.floor((image.data.length * 3) / 4)));
  return parts.join(" · ");
}

/**
 * The conversation as a model that takes no images must be sent it.
 *
 * Each image becomes a line of text saying it was left out, rather than
 * vanishing, so the model can say it cannot see it. The transcript itself keeps
 * the images.
 */
export function withoutImages(messages: Iterable<Message>, modelName: string): Message[] {
  const out: Message[] = [];
  for (const message of messages) {
    if (!hasImages(message)) {
      out.push(message);
      continue;
    }
    const content: ContentBlock[] = message.content.map((block) => {
      if (block.type === "image") return textBlock(`\n${omitted(block, modelName)}`);
      if (block.type === "tool_result" && block.images.length) {
        const notes = block.images.map((image) => omitted(image, modelName)).join("\n");
        return { ...block, content: `${block.content}\n\n${notes}`, images: [] };
      }
      return block;
    });
    out.push({ ...message, content });
  }
  return out;
}

function omitted(image: ImageBlock, modelName: string): string {
  return `[${image.label || "image"} omitted: ${modelName} does not accept image input]`;
}

function hasImages(message: Message): boolean {
  return message.content.some(
    (block) => block.type === "image" || (block.type === "tool_result" && block.images.length > 0),
  );
}

/**
 * Scale to fit, then encode small enough. PNG first, since that is what keeps
 * text crisp; JPEG when PNG is too big, and after that the image shrinks until
 * it fits.
 */
async function reencode(sharp: SharpFactory, data: Buffer, label: string): Promise<ImageBlock> {
  // `rotate()` with no angle applies the EXIF orientation and drops the tag.
  const upright = await sharp(data, { limitInputPixels: MAX_SOURCE_PIXELS, pages: 1 })
    .rotate()
    .png()
    .toBuffer({ resolveWithObject: true });
  let frame: Buffer<ArrayBufferLike> = upright.data;
  let width = upright.info.width;
  let height = upright.info.height;

  if (Math.max(width, height) > MAX_EDGE)
    [frame, width, height] = await scaled(sharp, frame, MAX_EDGE / Math.max(width, height));

  let encoded: Buffer<ArrayBufferLike>;
  let mediaType: string;
  while (true) {
    encoded = await sharp(frame).png({ compressionLevel: 9 }).toBuffer();
    mediaType = "image/png";
    if (encoded.length > MAX_BYTES) {
      // JPEG has no alpha: transparency is flattened onto white, not black.
      encoded = await sharp(frame)
        .flatten({ background: "#ffffff" })
        .jpeg({ quality: JPEG_QUALITY, mozjpeg: true })
        .toBuffer();
      mediaType = "image/jpeg";
    }
    if (encoded.length <= MAX_BYTES || Math.min(width, height) <= 64) break;
    [frame, width, height] = await scaled(sharp, frame, 0.75);
  }
  return imageBlock(mediaType, encoded.toString("base64"), { width, height, label });
}

async function scaled(
  sharp: SharpFactory,
  frame: Buffer,
  factor: number,
): Promise<[Buffer<ArrayBufferLike>, number, number]> {
  const meta = await sharp(frame).metadata();
  const width = Math.max(1, pyRound((meta.width ?? 1) * factor));
  const height = Math.max(1, pyRound((meta.height ?? 1) * factor));
  const resized = await sharp(frame)
    .resize(width, height, { kernel: "lanczos3", fit: "fill" })
    .png()
    .toBuffer();
  return [resized, width, height];
}

export function size(count: number): string {
  if (count < 1000) return `${count} B`;
  if (count < 1_000_000) return `${formatFixed(count / 1000, 0)} kB`;
  return `${formatFixed(count / 1_000_000, 1)} MB`;
}
