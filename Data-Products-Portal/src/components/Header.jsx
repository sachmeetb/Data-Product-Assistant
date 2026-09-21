import React from 'react';
import { LogOut } from 'lucide-react';

export default function Header({ user, onSignOut, activeView, setActiveView, currentProduct, setCurrentProduct }) {
  return (
    <header className="bg-white border-b sticky top-0 z-30 shadow-xs border-[#E2DCE8]">
      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 h-16 flex items-center justify-between">
        {/* Logo & Product Title */}
        <div className="flex items-center gap-3">
          <button 
            onClick={() => setActiveView("hub")}
            className="flex items-center gap-2 text-left hover:opacity-85 transition"
            title="Return to Product Selection Hub"
          >
            <div
              className="w-8 h-8 rounded-lg flex items-center justify-center font-black text-sm text-white shadow-xs bg-[#A100FF]"
            >
              &gt;
            </div>
            <div>
              <div className="text-sm font-bold leading-tight text-[#0F0A14]">
                Accenture <span className="font-normal text-gray-400">|</span> Data Products Hub
              </div>
              <div className="text-[10.5px] text-gray-400">
                BFSI Medallion Architecture (Silver & Gold)
              </div>
            </div>
          </button>

          {/* View Breadcrumb badge if in workspace */}
          {activeView === "workspace" && currentProduct && (
            <div className="hidden sm:flex items-center gap-2 ml-4 pl-4 border-l border-gray-200">
              <span className="text-xs text-gray-400">Viewing:</span>
              <span 
                className="text-xs font-semibold px-2.5 py-0.5 rounded-full"
                style={{ background: currentProduct.badgeBg, color: currentProduct.badgeColor }}
              >
                {currentProduct.title}
              </span>
            </div>
          )}
        </div>

        {/* Actions & User profile */}
        <div className="flex items-center gap-3">
          {/* Navigation toggle */}
          <div className="bg-gray-100 p-0.5 rounded-lg flex items-center text-xs">
            <button
              onClick={() => setActiveView("hub")}
              className={`px-3 py-1.5 rounded-md font-semibold transition ${
                activeView === "hub" 
                  ? "bg-white text-purple-900 shadow-xs" 
                  : "text-gray-600 hover:text-gray-900"
              }`}
            >
              Products Hub
            </button>
            <button
              onClick={() => {
                setActiveView("workspace");
              }}
              className={`px-3 py-1.5 rounded-md font-semibold transition flex items-center gap-1.5 ${
                activeView === "workspace" 
                  ? "bg-white text-purple-900 shadow-xs" 
                  : "text-gray-600 hover:text-gray-900"
              }`}
            >
              <span>Workspace</span>
              {activeView === "workspace" && (
                <span className="w-1.5 h-1.5 rounded-full bg-purple-600"></span>
              )}
            </button>
          </div>

          {/* User Chip & Sign Out */}
          <div className="flex items-center gap-2 pl-3 border-l border-gray-200">
            <div 
              className="w-8 h-8 rounded-full flex items-center justify-center text-xs font-bold text-purple-800 bg-[#F3E6FF]"
              title={user.email}
            >
              {user.initials}
            </div>
            <div className="hidden md:block text-left">
              <div className="text-xs font-semibold text-gray-800 leading-tight">{user.name}</div>
              <div className="text-[10px] text-gray-400 truncate max-w-[130px]">{user.email}</div>
            </div>
            <button
              onClick={onSignOut}
              title="Sign Out"
              className="ml-1 p-1.5 text-gray-400 hover:text-red-600 rounded-md hover:bg-red-50 transition"
            >
              <LogOut size={16} />
            </button>
          </div>
        </div>
      </div>
    </header>
  );
}
