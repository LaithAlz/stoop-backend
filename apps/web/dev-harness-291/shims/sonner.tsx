export const NOTICES: string[] = [];
export const toast = Object.assign((m: string) => { NOTICES.push(String(m)); }, {
  success: (m: string) => NOTICES.push("success:" + m),
  error: (m: string) => NOTICES.push("error:" + m),
});
export function Toaster() { return null; }
