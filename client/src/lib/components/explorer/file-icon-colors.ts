/** Category color palette shared between StyledFileIcon, TextPreview, and other modules */
export const CATEGORY_COLORS: Record<string, { fill: string; text: string }> = {
  // Code
  py: { fill: "#3572A5", text: "PY" },
  pyw: { fill: "#3572A5", text: "PY" },
  ts: { fill: "#3178C6", text: "TS" },
  tsx: { fill: "#3178C6", text: "TSX" },
  js: { fill: "#F7DF1E", text: "JS" },
  jsx: { fill: "#F7DF1E", text: "JSX" },
  rs: { fill: "#DEA584", text: "RS" },
  go: { fill: "#00ADD8", text: "GO" },
  c: { fill: "#555555", text: "C" },
  cpp: { fill: "#004482", text: "C++" },
  h: { fill: "#004482", text: "H" },
  java: { fill: "#B07219", text: "JAVA" },
  kt: { fill: "#A97BFF", text: "KT" },
  swift: { fill: "#F05138", text: "SWIFT" },
  rb: { fill: "#CC342D", text: "RB" },
  php: { fill: "#4F5D95", text: "PHP" },
  cs: { fill: "#68217A", text: "C#" },
  dart: { fill: "#00B4AB", text: "DART" },
  svelte: { fill: "#FF3E00", text: "SVLT" },
  vue: { fill: "#42B883", text: "VUE" },
  html: { fill: "#E34F26", text: "HTML" },
  css: { fill: "#1572B6", text: "CSS" },
  scss: { fill: "#CC6699", text: "SCSS" },
  less: { fill: "#1D365D", text: "LESS" },
  sh: { fill: "#4EAA25", text: "SH" },
  bash: { fill: "#4EAA25", text: "SH" },
  zsh: { fill: "#4EAA25", text: "ZSH" },
  sql: { fill: "#E38C00", text: "SQL" },
  ipynb: { fill: "#F37626", text: "NB" },

  // 3D Models
  stl: { fill: "#14B8A6", text: "STL" },
  obj: { fill: "#14B8A6", text: "OBJ" },
  gltf: { fill: "#14B8A6", text: "3D" },
  glb: { fill: "#14B8A6", text: "3D" },

  // Documents
  pdf: { fill: "#E53E3E", text: "PDF" },
  doc: { fill: "#2B579A", text: "DOC" },
  docx: { fill: "#2B579A", text: "DOCX" },
  rtf: { fill: "#2B579A", text: "RTF" },
  odt: { fill: "#2B579A", text: "ODT" },
  md: { fill: "#6B7280", text: "MD" },
  txt: { fill: "#6B7280", text: "TXT" },
  log: { fill: "#6B7280", text: "LOG" },

  // Spreadsheets
  xlsx: { fill: "#217346", text: "XLSX" },
  xls: { fill: "#217346", text: "XLS" },
  csv: { fill: "#217346", text: "CSV" },
  ods: { fill: "#217346", text: "ODS" },

  // Presentations
  pptx: { fill: "#B7472A", text: "PPTX" },
  ppt: { fill: "#B7472A", text: "PPT" },
  odp: { fill: "#B7472A", text: "ODP" },

  // Images
  png: { fill: "#9333EA", text: "PNG" },
  jpg: { fill: "#9333EA", text: "JPG" },
  jpeg: { fill: "#9333EA", text: "JPEG" },
  gif: { fill: "#9333EA", text: "GIF" },
  svg: { fill: "#9333EA", text: "SVG" },
  webp: { fill: "#9333EA", text: "WEBP" },
  bmp: { fill: "#9333EA", text: "BMP" },
  ico: { fill: "#9333EA", text: "ICO" },
  tiff: { fill: "#9333EA", text: "TIFF" },

  // Video
  mp4: { fill: "#EA580C", text: "MP4" },
  mov: { fill: "#EA580C", text: "MOV" },
  avi: { fill: "#EA580C", text: "AVI" },
  webm: { fill: "#EA580C", text: "WEBM" },
  mkv: { fill: "#EA580C", text: "MKV" },
  flv: { fill: "#EA580C", text: "FLV" },

  // Audio
  mp3: { fill: "#EC4899", text: "MP3" },
  wav: { fill: "#EC4899", text: "WAV" },
  flac: { fill: "#EC4899", text: "FLAC" },
  ogg: { fill: "#EC4899", text: "OGG" },
  aac: { fill: "#EC4899", text: "AAC" },
  m4a: { fill: "#EC4899", text: "M4A" },

  // Archives
  zip: { fill: "#B45309", text: "ZIP" },
  tar: { fill: "#B45309", text: "TAR" },
  gz: { fill: "#B45309", text: "GZ" },
  rar: { fill: "#B45309", text: "RAR" },
  "7z": { fill: "#B45309", text: "7Z" },
  bz2: { fill: "#B45309", text: "BZ2" },

  // Data / Config
  json: { fill: "#CBB133", text: "JSON" },
  yaml: { fill: "#6B8E23", text: "YAML" },
  yml: { fill: "#6B8E23", text: "YML" },
  toml: { fill: "#9CA3AF", text: "TOML" },
  xml: { fill: "#6B7280", text: "XML" },
  ini: { fill: "#9CA3AF", text: "INI" },
  env: { fill: "#9CA3AF", text: "ENV" },
  lock: { fill: "#9CA3AF", text: "LOCK" },

  // Executables / binaries
  exe: { fill: "#374151", text: "EXE" },
  dll: { fill: "#374151", text: "DLL" },
  so: { fill: "#374151", text: "SO" },
  dmg: { fill: "#374151", text: "DMG" },
  msi: { fill: "#374151", text: "MSI" },
  deb: { fill: "#374151", text: "DEB" },
  rpm: { fill: "#374151", text: "RPM" },
  wasm: { fill: "#654FF0", text: "WASM" },
};

export const DEFAULT_COLOR = { fill: "#6B7280", text: "" };

/** Extensions that are safe to preview as text */
export const TEXT_PREVIEW_EXTENSIONS = new Set([
  // Code
  "py", "pyw", "ts", "tsx", "js", "jsx", "rs", "go", "c", "cpp", "h", "hpp",
  "java", "kt", "swift", "rb", "php", "cs", "dart", "svelte", "vue",
  "html", "css", "scss", "less", "sh", "bash", "zsh", "sql", "r", "lua",
  "zig", "nim", "ex", "exs", "erl", "hs", "ml", "clj", "scala", "groovy",
  // Documents / text
  "md", "txt", "log", "rst", "org", "adoc",
  // Data / Config
  "json", "yaml", "yml", "toml", "xml", "ini", "env", "cfg", "conf",
  "csv", "tsv", "properties",
  // Build / CI
  "makefile", "dockerfile", "gitignore", "gitattributes", "editorconfig",
  "lock",
]);

/** Check if an extension supports text content preview */
export function isTextPreviewable(ext: string): boolean {
  return TEXT_PREVIEW_EXTENSIONS.has(ext.toLowerCase());
}

/** Check if an extension is a PDF */
export function isPdfFile(ext: string): boolean {
  return ext.toLowerCase() === "pdf";
}

/** Get category color for an extension */
export function getCategoryColor(ext: string): { fill: string; text: string } {
  const lower = ext.toLowerCase();
  const cat = CATEGORY_COLORS[lower];
  if (cat) return cat;
  if (lower) return { fill: DEFAULT_COLOR.fill, text: lower.toUpperCase().slice(0, 4) };
  return DEFAULT_COLOR;
}
