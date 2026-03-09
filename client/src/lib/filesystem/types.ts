export interface FileEntry {
  name: string;
  path: string;
  isDir: boolean;
  size: number;
  modified: number;
  extension: string;
  source: "local" | "remote" | "cloud";
}

export interface DefaultDirs {
  home: string;
  documents: string;
  downloads: string;
  desktop: string;
}

export interface FileChangeEvent {
  path: string;
  kind: "create" | "modify" | "delete";
  is_dir: boolean;
}

export interface RecursiveSearchResult {
  entries: FileEntry[];
  totalScanned: number;
  truncated: boolean;
}

export interface FileSystemProvider {
  scheme: "local" | "remote" | "cloud";
  readDir(path: string): Promise<FileEntry[]>;
  readFileText(path: string): Promise<string>;
  writeFile(path: string, content: string): Promise<void>;
  deleteFile(path: string, recursive?: boolean): Promise<void>;
  rename(oldPath: string, newPath: string): Promise<void>;
  stat(path: string): Promise<FileEntry>;
  createDir(path: string): Promise<void>;
  exists(path: string): Promise<boolean>;
  watch(path: string, callback: (event: FileChangeEvent) => void): Promise<() => void>;
  getDefaultDirs(): Promise<DefaultDirs>;
}
