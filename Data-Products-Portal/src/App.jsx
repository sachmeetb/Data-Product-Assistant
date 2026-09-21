import React, { useState } from 'react';
import LoginScreen from './components/LoginScreen.jsx';
import Header from './components/Header.jsx';
import ProductHub from './components/ProductHub.jsx';
import WorkspaceViewer from './components/WorkspaceViewer.jsx';

export const PRODUCTS = [
  {
    id: "gold",
    title: "Gold Product Assistant (BP - Product)",
    url: "https://proxy-dp-assistant-v2-frontend-preview-743146199.us-central1.run.app",
    gradient: "linear-gradient(135deg, #451A03 0%, #78350F 50%, #B45309 100%)"
  },
  {
    id: "silver",
    title: "Silver Product Assistant (Banking)",
    url: "https://proxy-bfsi-silver-frontend-v1-1-743146199.us-central1.run.app/",
    gradient: "linear-gradient(135deg, #1E293B 0%, #334155 100%)"
  }
];

export default function App() {
  const [user, setUser] = useState(null);
  const [activeView, setActiveView] = useState("hub");
  const [currentProduct, setCurrentProduct] = useState(PRODUCTS[0]);

  if (!user) {
    return <LoginScreen onSignIn={u => setUser(u)} />;
  }

  return (
    <div className="h-screen flex flex-col overflow-hidden bg-[#F7F4FA]">
      <Header
        user={user}
        onSignOut={() => setUser(null)}
        activeView={activeView}
        setActiveView={setActiveView}
        currentProduct={currentProduct}
      />

      {activeView === "hub" ? (
        <ProductHub
          onSelectProduct={(prod, targetView) => {
            setCurrentProduct(prod);
            if (targetView) setActiveView(targetView);
          }}
        />
      ) : (
        <WorkspaceViewer
          currentProduct={currentProduct}
          onSwitchProduct={p => setCurrentProduct(p)}
          onBackToHub={() => setActiveView("hub")}
        />
      )}
    </div>
  );
}
