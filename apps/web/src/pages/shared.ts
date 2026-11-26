import { useState } from "react";

/** Remember the last task each page started, so coming back shows its progress. */
export function useStoredTask(page: string): [string | null, (id: string | null) => void] {
  const key = `gpudock.task.${page}`;
  const [id, setId] = useState<string | null>(() => sessionStorage.getItem(key));
  return [
    id,
    (next) => {
      if (next) sessionStorage.setItem(key, next);
      else sessionStorage.removeItem(key);
      setId(next);
    },
  ];
}
