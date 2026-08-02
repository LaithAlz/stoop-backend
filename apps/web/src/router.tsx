import { createRouter } from "@tanstack/react-router";
import { routeTree } from "./routeTree.gen";
import { createQueryClient } from "./api/queryClient";

export const getRouter = () => {
  const queryClient = createQueryClient();

  const router = createRouter({
    routeTree,
    context: { queryClient },
    scrollRestoration: true,
    defaultPreloadStaleTime: 0,
  });

  return router;
};
