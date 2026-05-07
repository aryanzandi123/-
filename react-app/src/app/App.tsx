import { BrowserRouter, Route, Routes } from "react-router-dom";

import { ErrorBoundary } from "@/app/routes/ErrorBoundary";
import { Index } from "@/app/routes/Index";
import { Visualize } from "@/app/routes/Visualize";
import { Workspace } from "@/app/routes/Workspace";

export function App(): JSX.Element {
  return (
    <ErrorBoundary>
      <BrowserRouter>
        <Routes>
          <Route path="/" element={<Index />} />
          <Route path="/visualize/:protein" element={<Visualize />} />
          <Route path="/api/visualize/:protein" element={<Visualize />} />
          <Route path="/workspace/:proteinList" element={<Workspace />} />
          <Route path="*" element={<Index />} />
        </Routes>
      </BrowserRouter>
    </ErrorBoundary>
  );
}
