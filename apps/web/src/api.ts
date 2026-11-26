// Talks to the local gpudock server. The per-launch token arrives in the URL
// fragment (#token=...), is kept in sessionStorage and removed from the URL.

const TOKEN_KEY = "gpudock.token";

function readToken(): string | null {
  const hash = new URLSearchParams(window.location.hash.slice(1));
  const fromHash = hash.get("token");
  if (fromHash) {
    sessionStorage.setItem(TOKEN_KEY, fromHash);
    history.replaceState(null, "", window.location.pathname + window.location.search);
    return fromHash;
  }
  return sessionStorage.getItem(TOKEN_KEY);
}

export const token = readToken();

// A token pasted into an already-open tab only changes the fragment, which
// doesn't reload the page; pick it up and start over.
window.addEventListener("hashchange", () => {
  if (new URLSearchParams(window.location.hash.slice(1)).get("token")) {
    readToken();
    window.location.reload();
  }
});

export class ApiError extends Error {
  status: number;
  code: string;
  constructor(status: number, code: string, message: string) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("Authorization", `Bearer ${token ?? ""}`);
  if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  let resp: Response;
  try {
    resp = await fetch(path, { ...init, headers });
  } catch {
    throw new ApiError(0, "offline", "连不上 GPUDock 本地服务，确认 gpudock 命令还在运行。");
  }
  const text = await resp.text();
  const data = text ? JSON.parse(text) : null;
  if (!resp.ok) {
    throw new ApiError(resp.status, data?.code ?? "error", data?.error ?? `请求失败（HTTP ${resp.status}）`);
  }
  return data as T;
}

export const post = <T,>(path: string, body?: unknown) =>
  api<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });
export const put = <T,>(path: string, body: unknown) => api<T>(path, { method: "PUT", body: JSON.stringify(body) });
export const del = <T,>(path: string) => api<T>(path, { method: "DELETE" });

/** Upload files to the local server with progress; returns upload ids/names. */
export function uploadFiles(files: File[], onProgress?: (fraction: number) => void): Promise<import("./types").UploadInfo[]> {
  return new Promise((resolve, reject) => {
    const form = new FormData();
    for (const f of files) form.append("files", f, f.name);
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/uploads");
    xhr.setRequestHeader("Authorization", `Bearer ${token ?? ""}`);
    xhr.upload.onprogress = (e) => e.lengthComputable && onProgress?.(e.loaded / e.total);
    xhr.onload = () => {
      try {
        const data = JSON.parse(xhr.responseText || "null");
        if (xhr.status >= 400) reject(new ApiError(xhr.status, data?.code ?? "error", data?.error ?? "上传失败"));
        else resolve(data);
      } catch {
        reject(new ApiError(xhr.status, "error", "上传失败"));
      }
    };
    xhr.onerror = () => reject(new ApiError(0, "offline", "上传失败：连不上 GPUDock 本地服务"));
    xhr.send(form);
  });
}

/** Fetch a task output as an object URL (downloads need the auth header). */
export async function fileUrl(taskId: string, name: string): Promise<string> {
  const resp = await fetch(`/api/tasks/${taskId}/files/${encodeURIComponent(name)}`, {
    headers: { Authorization: `Bearer ${token ?? ""}` },
  });
  if (!resp.ok) throw new ApiError(resp.status, "error", "文件读取失败");
  return URL.createObjectURL(await resp.blob());
}

export async function saveFile(taskId: string, name: string): Promise<void> {
  const url = await fileUrl(taskId, name);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 60_000);
}
