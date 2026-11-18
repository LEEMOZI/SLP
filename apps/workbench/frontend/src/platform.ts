import { open as openDialog } from "@tauri-apps/plugin-dialog";
import { readFile } from "@tauri-apps/plugin-fs";

export const isTauri = typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;

/** Tauri 环境：原生对话框选文件并转成 File；浏览器环境返回 null（走 <input type="file"> 回退） */
export async function pickFile(accept: string[]): Promise<File | null> {
  if (!isTauri) return null;
  const exts = accept.map((a) => a.replace(/^\./, "").toUpperCase());
  const selected = await openDialog({
    multiple: false,
    filters: [{ name: "案件材料", extensions: exts }],
  });
  if (typeof selected !== "string") return null;
  const name = selected.split("/").pop() ?? "file";
  const bytes = await readFile(selected);
  return new File([bytes], name);
}
