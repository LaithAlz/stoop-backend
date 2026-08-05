import type { ReactNode } from "react";
export function createFileRoute(_path: string) {
  return (options: any) => ({ options, useParams: () => ({ id: "case-x" }) });
}
export function createRootRoute(options: any) { return { options }; }
export function Link({ children, className, ...rest }: any) {
  return <a className={className} href="#" {...(rest["aria-label"] ? { "aria-label": rest["aria-label"] } : {})}>{children}</a>;
}
export function Outlet() { return null; }
export function useNavigate() { return async () => {}; }
export function useRouter() { return {} as any; }
