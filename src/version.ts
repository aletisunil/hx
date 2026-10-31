declare const __HX_VERSION__: string;
declare const __HX_PACKAGE__: string;

/** This build's version, from `package.json`, set by the bundler. */
export const VERSION: string = __HX_VERSION__;

/** The npm package HX is published as, from `package.json`. */
export const PACKAGE: string = __HX_PACKAGE__;
