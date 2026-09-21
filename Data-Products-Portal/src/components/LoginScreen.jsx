import React, { useState } from 'react';
import { Shield, Loader2, CheckCircle2, AlertTriangle, Lock } from 'lucide-react';

export default function LoginScreen({ onSignIn }) {
  const [email, setEmail] = useState("r.yuva.kishore@accenture.com");
  const [authState, setAuthState] = useState("idle");
  const [error, setError] = useState(null);

  const quickPresets = [
    { label: "R Yuva Kishore", email: "r.yuva.kishore@accenture.com", role: "Lead Data Architect" },
    { label: "Sarah Jenkins", email: "sarah.jenkins@accenture.com", role: "Campaign Manager" },
    { label: "Alex Rivera", email: "alex.rivera@accenture.com", role: "Data Product Owner" }
  ];

  const submit = (targetEmail) => {
    setError(null);
    const emailToValidate = (targetEmail || email || "").trim().toLowerCase();
    if (!emailToValidate.endsWith("@accenture.com")) {
      setError("Please use your @accenture.com corporate email.");
      return;
    }
    setAuthState("authenticating");
    setTimeout(() => setAuthState("success"), 850);
    setTimeout(() => {
      const namePart = emailToValidate.split("@")[0].split(".").map(s => s.charAt(0).toUpperCase() + s.slice(1)).join(" ");
      onSignIn({
        email: emailToValidate,
        name: namePart || "Accenture Professional",
        initials: (namePart.split(" ").map(p => p[0]).join("") || "AC").slice(0, 2).toUpperCase()
      });
    }, 1500);
  };

  return (
    <div
      className="min-h-screen flex items-center justify-center p-4 sm:p-6"
      style={{
        background: `radial-gradient(circle at 25% 10%, #F3E6FF 0%, #F7F4FA 55%, white 100%)`
      }}
    >
      <div 
        className="w-full max-w-4xl bg-white rounded-2xl overflow-hidden flex flex-col md:flex-row shadow-2xl border border-[#E2DCE8]"
      >
        {/* Left Brand Panel */}
        <div
          className="md:w-5/12 p-8 sm:p-10 text-white flex flex-col justify-between relative overflow-hidden"
          style={{
            background: `linear-gradient(155deg, #460073 0%, #2E004D 100%)`
          }}
        >
          <div>
            <div className="flex items-center gap-2.5">
              <div
                className="w-9 h-9 rounded-lg flex items-center justify-center font-extrabold text-sm text-white"
                style={{ background: "#FF6B00" }}
              >
                &gt;
              </div>
              <div>
                <div className="text-xs tracking-widest font-bold text-purple-200">ACCENTURE</div>
                <div className="text-xs font-semibold opacity-75">Data Products Platform</div>
              </div>
            </div>

            <h1 className="text-2xl sm:text-3xl font-bold mt-8 leading-tight tracking-tight">
              From a business question to a governed data product — in one conversation.
            </h1>

            <p className="mt-4 text-xs sm:text-sm text-purple-100 leading-relaxed opacity-90">
              Sign in with your Accenture account to discover, design and build data products with the agentic platform.
            </p>

            <div className="mt-6 space-y-2.5">
              <div className="flex items-center gap-2 text-xs text-purple-200">
                <CheckCircle2 size={13} className="text-[#FF6B00]" />
                <span>Gold Product Assistant (BP - Product)</span>
              </div>
              <div className="flex items-center gap-2 text-xs text-purple-200">
                <CheckCircle2 size={13} className="text-[#FF6B00]" />
                <span>Silver Product Assistant (Banking)</span>
              </div>
            </div>
          </div>

          <div className="mt-10 pt-4 border-t border-purple-800 text-xs text-purple-300 flex items-center justify-between">
            <span>Enterprise Medallion Platform</span>
            <span className="px-2 py-0.5 rounded text-[10px] bg-purple-900 font-mono">v2.4</span>
          </div>
        </div>

        {/* Right Form Panel */}
        <div className="md:w-7/12 p-8 sm:p-10 flex flex-col justify-between bg-white">
          <div>
            <div className="flex items-center justify-between">
              <span className="text-[11px] font-bold tracking-widest uppercase text-[#A100FF]">
                Single Sign-On (SSO)
              </span>
              <span className="inline-flex items-center gap-1 text-[11px] font-medium text-emerald-700 bg-emerald-50 px-2 py-0.5 rounded-full border border-emerald-200">
                <span className="w-1.5 h-1.5 rounded-full bg-emerald-500 animate-pulse"></span> Systems Active
              </span>
            </div>

            <h2 className="text-2xl font-bold mt-2 text-[#0F0A14]">
              Welcome back
            </h2>
            <p className="text-xs text-gray-500 mt-1">
              Authenticate with your corporate credentials to access the Silver and Gold workspaces.
            </p>

            {/* Email Input */}
            <div className="mt-6">
              <label className="block text-[11px] font-bold uppercase tracking-wider text-gray-600 mb-1.5">
                Corporate Email
              </label>
              <input
                type="email"
                value={email}
                onChange={e => setEmail(e.target.value)}
                onKeyDown={e => e.key === 'Enter' && submit()}
                disabled={authState !== "idle"}
                placeholder="name.surname@accenture.com"
                className="w-full px-3.5 py-2.5 rounded-lg text-sm border focus:outline-none transition border-[#E2DCE8]"
                style={{
                  background: authState !== "idle" ? "#F9FAFB" : "white"
                }}
              />
              {error && (
                <div className="text-xs text-red-600 mt-1.5 flex items-center gap-1">
                  <AlertTriangle size={12} /> {error}
                </div>
              )}

              {/* SSO Button */}
              <button
                onClick={() => submit()}
                disabled={authState !== "idle"}
                className="w-full mt-4 py-3 px-4 rounded-lg font-semibold text-sm text-white flex items-center justify-center gap-2 transition duration-200 shadow-md bg-[#A100FF] hover:bg-[#7800C4]"
              >
                {authState === "idle" && (
                  <>
                    <Shield size={16} /> Sign in with Accenture SSO
                  </>
                )}
                {authState === "authenticating" && (
                  <>
                    <Loader2 size={16} className="animate-spin" /> Verifying Credentials…
                  </>
                )}
                {authState === "success" && (
                  <>
                    <CheckCircle2 size={16} /> Access Granted · Redirecting…
                  </>
                )}
              </button>

              <div className="flex items-center gap-2 mt-3 text-[11px] text-gray-400 justify-center">
                <Lock size={11} className="text-[#A100FF]" />
                <span>Microsoft Entra ID · FIDO2 / MFA Enforced</span>
              </div>
            </div>

            {/* Persona quick select */}
            <div className="mt-6 pt-5 border-t border-gray-100">
              <div className="text-[11px] font-semibold text-gray-400 uppercase tracking-wider mb-2">
                Quick Demo Profiles:
              </div>
              <div className="grid grid-cols-1 sm:grid-cols-3 gap-2">
                {quickPresets.map((p, idx) => (
                  <button
                    key={idx}
                    type="button"
                    onClick={() => {
                      setEmail(p.email);
                      submit(p.email);
                    }}
                    className="text-left p-2 rounded-lg border border-gray-200 hover:border-purple-300 hover:bg-purple-50/50 transition group"
                  >
                    <div className="text-xs font-semibold text-gray-800 group-hover:text-purple-700">{p.label}</div>
                    <div className="text-[10px] text-gray-400 truncate">{p.role}</div>
                  </button>
                ))}
              </div>
            </div>
          </div>

          <div className="mt-6 pt-4 border-t border-gray-100 flex items-center justify-between text-[11px] text-gray-400">
            <span>Accenture BFSI Practice</span>
            <a href="mailto:support@accenture.com" className="text-purple-600 hover:underline">IT Help Desk</a>
          </div>
        </div>
      </div>
    </div>
  );
}
