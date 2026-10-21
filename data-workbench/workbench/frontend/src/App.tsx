import { useEffect, useMemo, useState } from "react";
import { BrowserRouter, Navigate, Route, Routes, useLocation, useNavigate, useParams } from "react-router-dom";
import { AuthProvider, useAuth } from "./AuthContext";
import LoginPage from "./pages/LoginPage";
import ProjectListPage from "./pages/ProjectListPage";
import NewProjectPage from "./pages/NewProjectPage";
import ProjectDetailPage from "./pages/ProjectDetailPage";
import SettingsPage from "./pages/SettingsPage";
import MarketplacePage from "./pages/MarketplacePage";
import PersonaChooserPage from "./pages/PersonaChooserPage";
import ProductLandingPage from "./pages/product/ProductLandingPage";
import NewProductWizard from "./pages/product/NewProductWizard";
import EditSourceProductPanel from "./pages/product/EditSourceProductPanel";
import NewSourceProductWizard from "./pages/product/NewSourceProductWizard";
import MyProductsDashboard from "./pages/product/MyProductsDashboard";
import MarketplaceGapsPage from "./pages/product/MarketplaceGapsPage";
import PoValidationPage from "./pages/product/PoValidationPage";
import IngestExistingProductPage from "./pages/product/IngestExistingProductPage";
import DiscoveryPage from "./pages/product/DiscoveryPage";
import TemplateLibraryPage from "./pages/product/TemplateLibraryPage";
import TemplateDetailPage from "./pages/product/TemplateDetailPage";
import EstatePage from "./pages/product/EstatePage";
import FeasibilityPage from "./pages/product/FeasibilityPage";
import AssemblyPage from "./pages/product/AssemblyPage";
import IncomingQueuePage from "./pages/engineer/IncomingQueuePage";
import IntakeListPage from "./pages/intake/IntakeListPage";
import IntakeReviewPage from "./pages/intake/IntakeReviewPage";
import ConnectionsPage from "./pages/engineer/ConnectionsPage";
import SemanticRecommenderPage from "./pages/SemanticRecommenderPage";
import ProductShell from "./shells/ProductShell";
import EngineerShell from "./shells/EngineerShell";
import { RoleContext } from "./RoleContext";
import { engineerTheme, productTheme } from "./theme";
import { ConfirmProvider } from "./components/ConfirmProvider";
import type { Role } from "./types";

function LegacyProjectRedirect() {
  const { id } = useParams<{ id: string }>();
  return <Navigate to={`/engineer/projects/${id ?? ""}`} replace />;
}

function LegacyMarketplaceRedirect() {
  let target: "product" | "engineer" = "engineer";
  try {
    const pref = window.localStorage.getItem("workbench.preferred");
    if (pref === "product") target = "product";
  } catch {
    /* ignore storage errors */
  }
  return <Navigate to={`/${target}/marketplace`} replace />;
}

function LocationAwareRoutes() {
  return (
    <Routes>
      <Route path="/" element={<PersonaChooserPage />} />

      {/* Product Workbench */}
      <Route path="/product" element={<ProductShell />}>
        <Route index element={<ProductLandingPage />} />
        <Route path="marketplace" element={<MarketplacePage />} />
        <Route path="marketplace/:uri" element={<MarketplacePage />} />
        <Route path="my-products" element={<MyProductsDashboard />} />
        {/* Bare /product/new kept as a fallback that lands on the source-aligned
            wizard — the dashboard CTAs now route to /new/source or /new/consumer
            explicitly. */}
        <Route path="new" element={<NewSourceProductWizard />} />
        <Route path="new/source" element={<NewSourceProductWizard />} />
        <Route path="new/consumer" element={<NewProductWizard />} />
        <Route path="edit/:projectId" element={<NewProductWizard />} />
        <Route path="edit-source/:projectId" element={<EditSourceProductPanel />} />
        <Route path="validate/:projectId" element={<PoValidationPage />} />
        <Route path="ingest" element={<IngestExistingProductPage />} />
        <Route path="templates" element={<TemplateLibraryPage />} />
        <Route path="templates/:id" element={<TemplateDetailPage />} />
        <Route path="discovery" element={<DiscoveryPage />} />
        <Route path="estate" element={<EstatePage />} />
        <Route path="feasibility" element={<FeasibilityPage />} />
        <Route path="assembly/:id" element={<AssemblyPage />} />
        <Route
          path="intake"
          element={
            <IntakeListPage
              scenario="modernization"
              basePath="/product"
              accent={productTheme.accent}
              title="Modernization intake"
              blurb="Portfolios an external assessment tool pushed in. Review the proposed source- and consumer-aligned products, then approve to scaffold candidate projects you complete in the wizards."
            />
          }
        />
        <Route path="intake/:id" element={<IntakeReviewPage />} />
        <Route path="semantic-recommender" element={<SemanticRecommenderPage />} />
        <Route path="gaps" element={<MarketplaceGapsPage />} />
      </Route>

      {/* Engineering Workbench */}
      <Route path="/engineer" element={<EngineerShell />}>
        <Route index element={<ProjectListPage />} />
        <Route path="incoming" element={<IncomingQueuePage />} />
        <Route
          path="intake"
          element={
            <IntakeListPage
              scenario="migration"
              basePath="/engineer"
              accent={engineerTheme.accent}
              title="Migration intake"
              blurb="Migration recommendations an external assessment tool pushed in. Review the parsed blueprint, fill any gaps, then approve to scaffold a data-migration project."
            />
          }
        />
        <Route path="intake/:id" element={<IntakeReviewPage />} />
        <Route path="projects/new" element={<NewProjectPage />} />
        <Route path="projects/:id" element={<ProjectDetailPage />} />
        <Route path="marketplace" element={<MarketplacePage />} />
        <Route path="marketplace/:uri" element={<MarketplacePage />} />
        <Route path="semantic-recommender" element={<SemanticRecommenderPage />} />
        <Route path="connections" element={<ConnectionsPage />} />
        <Route path="settings" element={<SettingsPage />} />
      </Route>

      {/* Legacy URL redirects */}
      <Route path="/projects/new" element={<Navigate to="/engineer/projects/new" replace />} />
      <Route path="/projects/:id" element={<LegacyProjectRedirect />} />
      <Route path="/marketplace" element={<LegacyMarketplaceRedirect />} />
      <Route path="/settings" element={<Navigate to="/engineer/settings" replace />} />

      <Route path="/login" element={<LoginPage />} />

      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}

function hatForRole(accountRole: string | undefined): Role {
  return accountRole === "owner" ? "Data Product Owner" : "Data Engineer";
}

/** Gate the app behind login when auth is enabled; pass through untouched when
 *  auth is disabled (local dev). Also seeds the client-side Role from the
 *  authenticated account role. */
function RequireAuth({ children }: { children: React.ReactNode }) {
  const { ready, authEnabled, user } = useAuth();
  const location = useLocation();
  const navigate = useNavigate();

  useEffect(() => {
    if (ready && authEnabled && !user && location.pathname !== "/login") {
      navigate("/login", { replace: true });
    }
  }, [ready, authEnabled, user, location.pathname, navigate]);

  if (!ready) {
    return <div style={{ padding: 40, fontFamily: "system-ui", color: "#64748b" }}>Loading…</div>;
  }
  if (authEnabled && !user && location.pathname !== "/login") {
    return null; // redirect effect will fire
  }
  return <>{children}</>;
}

function ShellWithRole() {
  const { user } = useAuth();
  // Seed the initial client hat from the account role. The shells
  // (ProductShell / EngineerShell) re-assert the correct hat on mount when the
  // account role resolves, so no re-seeding effect is needed here.
  const [role, setRole] = useState<Role>(hatForRole(user?.role));

  const contextValue = useMemo(() => ({ role, setRole }), [role]);
  return (
    <RoleContext.Provider value={contextValue}>
      <RequireAuth>
        <LocationAwareRoutes />
      </RequireAuth>
    </RoleContext.Provider>
  );
}

function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        <ConfirmProvider>
          <ShellWithRole />
        </ConfirmProvider>
      </AuthProvider>
    </BrowserRouter>
  );
}

export default App;
