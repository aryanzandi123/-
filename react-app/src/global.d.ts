/* CSS Modules — typed-as-string-record so TS knows the import shape. */
declare module "*.module.css" {
  const classes: Readonly<Record<string, string>>;
  export default classes;
}
