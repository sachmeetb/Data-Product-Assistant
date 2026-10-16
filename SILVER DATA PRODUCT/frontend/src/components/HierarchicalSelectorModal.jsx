import React, { useState, useEffect, useMemo } from 'react';
import { fetchIndustries, fetchIndustryDomains } from '../api/catalog';

export default function HierarchicalSelectorModal({ isOpen, onClose, onApply }) {
  const [industries, setIndustries] = useState([]);
  const [selectedIndustryId, setSelectedIndustryId] = useState('energy_industrial');
  const [domains, setDomains] = useState([]);
  const [loading, setLoading] = useState(false);
  const [searchQuery, setSearchQuery] = useState('');
  
  // Selection state: map of table -> Set of attribute names
  // e.g. { 'water_quality_telemetry': Set(['asset_id', 'ph_level', ...]) }
  const [selectedAttributes, setSelectedAttributes] = useState({});
  const [expandedDomains, setExpandedDomains] = useState({});
  const [expandedTables, setExpandedTables] = useState({});

  useEffect(() => {
    if (!isOpen) return;
    async function loadIndustries() {
      try {
        const data = await fetchIndustries();
        if (data && data.industries) {
          setIndustries(data.industries);
          if (data.industries.length > 0 && !selectedIndustryId) {
            setSelectedIndustryId(data.industries[0].id);
          }
        }
      } catch (err) {
        console.error('Failed to load industries', err);
      }
    }
    loadIndustries();
  }, [isOpen]);

  useEffect(() => {
    if (!isOpen || !selectedIndustryId) return;
    async function loadDomains() {
      setLoading(true);
      try {
        const data = await fetchIndustryDomains(selectedIndustryId);
        if (data && data.domains) {
          setDomains(data.domains);
          // auto-expand all domains in this industry
          const expDom = {};
          const expTab = {};
          data.domains.forEach(d => {
            expDom[d.domain_name] = true;
            (d.entities || []).forEach(e => {
              expTab[`${d.domain_name}::${e.table_name}`] = true;
            });
          });
          setExpandedDomains(expDom);
          setExpandedTables(expTab);
        }
      } catch (err) {
        console.error('Failed to load domains', err);
      } finally {
        setLoading(false);
      }
    }
    loadDomains();
  }, [isOpen, selectedIndustryId]);

  const toggleDomain = (domainName) => {
    setExpandedDomains(prev => ({ ...prev, [domainName]: !prev[domainName] }));
  };

  const toggleTable = (key) => {
    setExpandedTables(prev => ({ ...prev, [key]: !prev[key] }));
  };

  const handleAttributeToggle = (tableName, colName) => {
    setSelectedAttributes(prev => {
      const current = prev[tableName] ? new Set(prev[tableName]) : new Set();
      if (current.has(colName)) {
        current.delete(colName);
      } else {
        current.add(colName);
      }
      return { ...prev, [tableName]: current };
    });
  };

  const handleTableToggleAll = (tableName, allCols) => {
    setSelectedAttributes(prev => {
      const current = prev[tableName] ? new Set(prev[tableName]) : new Set();
      const allSelected = allCols.every(c => current.has(c.column_name));
      if (allSelected) {
        const next = { ...prev };
        delete next[tableName];
        return next;
      } else {
        return { ...prev, [tableName]: new Set(allCols.map(c => c.column_name)) };
      }
    });
  };

  const handleSelectAllInIndustry = () => {
    const next = {};
    domains.forEach(d => {
      (d.entities || []).forEach(e => {
        next[e.table_name] = new Set((e.columns || []).map(c => c.column_name));
      });
    });
    setSelectedAttributes(next);
  };

  const handleClearSelection = () => {
    setSelectedAttributes({});
  };

  const totalSelectedAttributes = useMemo(() => {
    return Object.values(selectedAttributes).reduce((acc, s) => acc + (s ? s.size : 0), 0);
  }, [selectedAttributes]);

  const totalSelectedTables = useMemo(() => {
    return Object.values(selectedAttributes).filter(s => s && s.size > 0).length;
  }, [selectedAttributes]);

  const handleApplySelection = () => {
    const currentIndustry = industries.find(i => i.id === selectedIndustryId);
    const lines = [];
    lines.push(`Augment Conformed Silver Model for Industry: **${currentIndustry ? currentIndustry.name : selectedIndustryId}**`);
    
    Object.entries(selectedAttributes).forEach(([tbl, colsSet]) => {
      if (colsSet && colsSet.size > 0) {
        lines.push(`- **Table \`${tbl}\`**: ${Array.from(colsSet).map(c => `\`${c}\``).join(', ')}`);
      }
    });

    const formattedPrompt = lines.join('\n');
    onApply(formattedPrompt, {
      industry_id: selectedIndustryId,
      selected_attributes: selectedAttributes,
      table_count: totalSelectedTables,
      attribute_count: totalSelectedAttributes,
    });
    onClose();
  };

  if (!isOpen) return null;

  const q = searchQuery.toLowerCase().trim();

  return (
    <div style={{
      position: 'fixed',
      top: 0,
      left: 0,
      right: 0,
      bottom: 0,
      backgroundColor: 'rgba(15, 23, 42, 0.65)',
      backdropFilter: 'blur(3px)',
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'center',
      zIndex: 9999,
      padding: 20,
    }}>
      <div style={{
        background: '#ffffff',
        borderRadius: 12,
        width: '100%',
        maxWidth: 950,
        height: '88vh',
        display: 'flex',
        flexDirection: 'column',
        boxShadow: '0 25px 50px -12px rgba(0, 0, 0, 0.25)',
        overflow: 'hidden',
        border: '1px solid #e2e8f0',
      }}>
        {/* Header */}
        <div style={{
          padding: '16px 24px',
          background: 'linear-gradient(135deg, #460073 0%, #7500C0 100%)',
          color: '#ffffff',
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
        }}>
          <div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <span style={{ fontSize: 18 }}>🌲</span>
              <h3 style={{ margin: 0, fontSize: 16, fontWeight: 700, letterSpacing: '-0.01em' }}>
                Hierarchical Domain & Schema Navigator
              </h3>
              <span style={{
                background: 'rgba(255,255,255,0.2)',
                fontSize: 10.5,
                fontWeight: 600,
                padding: '2px 8px',
                borderRadius: 12,
              }}>
                Industry → Domain → Table → Attributes
              </span>
            </div>
            <p style={{ margin: '4px 0 0', fontSize: 11.5, opacity: 0.9 }}>
              Select specific canonical tables and attributes to augment your Silver Data Contract.
            </p>
          </div>
          <button
            onClick={onClose}
            style={{
              background: 'rgba(255,255,255,0.15)',
              border: 'none',
              color: '#ffffff',
              fontSize: 16,
              borderRadius: 6,
              width: 32,
              height: 32,
              cursor: 'pointer',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
            }}
          >
            ✕
          </button>
        </div>

        {/* Industry Pill Selector Bar */}
        <div style={{
          padding: '10px 24px',
          background: '#f8fafc',
          borderBottom: '1px solid #e2e8f0',
          display: 'flex',
          alignItems: 'center',
          gap: 8,
          overflowX: 'auto',
        }}>
          <span style={{ fontSize: 11, fontWeight: 700, color: '#64748b', textTransform: 'uppercase', whiteSpace: 'nowrap' }}>
            Industry:
          </span>
          {industries.map(ind => (
            <button
              key={ind.id}
              onClick={() => setSelectedIndustryId(ind.id)}
              style={{
                background: selectedIndustryId === ind.id ? '#7500C0' : '#ffffff',
                color: selectedIndustryId === ind.id ? '#ffffff' : '#334155',
                border: `1px solid ${selectedIndustryId === ind.id ? '#7500C0' : '#cbd5e1'}`,
                borderRadius: 20,
                padding: '4px 12px',
                fontSize: 11.5,
                fontWeight: selectedIndustryId === ind.id ? 700 : 500,
                cursor: 'pointer',
                whiteSpace: 'nowrap',
                display: 'flex',
                alignItems: 'center',
                gap: 5,
                boxShadow: selectedIndustryId === ind.id ? '0 2px 4px rgba(117,0,192,0.2)' : 'none',
                transition: 'all 0.15s ease',
              }}
            >
              <span>{ind.icon || '🏷️'}</span>
              <span>{ind.name}</span>
            </button>
          ))}
        </div>

        {/* Filter and Quick Action Bar */}
        <div style={{
          padding: '8px 24px',
          background: '#ffffff',
          borderBottom: '1px solid #f1f5f9',
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          gap: 16,
        }}>
          <div style={{ position: 'relative', flex: 1, maxWidth: 420 }}>
            <input
              type="text"
              placeholder="Search domains, tables, or column names..."
              value={searchQuery}
              onChange={e => setSearchQuery(e.target.value)}
              style={{
                width: '100%',
                padding: '6px 12px 6px 30px',
                fontSize: 12,
                border: '1px solid #cbd5e1',
                borderRadius: 6,
                outline: 'none',
              }}
            />
            <span style={{ position: 'absolute', left: 10, top: 6, fontSize: 12, color: '#94a3b8' }}>🔍</span>
          </div>

          <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
            <button
              type="button"
              onClick={handleSelectAllInIndustry}
              style={{
                background: '#f1f5f9',
                border: '1px solid #cbd5e1',
                borderRadius: 6,
                padding: '5px 10px',
                fontSize: 11,
                fontWeight: 600,
                color: '#334155',
                cursor: 'pointer',
              }}
            >
              Select All in Industry
            </button>
            <button
              type="button"
              onClick={handleClearSelection}
              style={{
                background: '#f1f5f9',
                border: '1px solid #cbd5e1',
                borderRadius: 6,
                padding: '5px 10px',
                fontSize: 11,
                fontWeight: 600,
                color: '#dc2626',
                cursor: 'pointer',
              }}
            >
              Clear Selection
            </button>
          </div>
        </div>

        {/* Content Hierarchy Tree */}
        <div style={{ flex: 1, overflowY: 'auto', padding: '16px 24px', background: '#fafbfc' }}>
          {loading ? (
            <div style={{ textAlign: 'center', padding: '40px 0', color: '#64748b' }}>
              Loading industry domain models...
            </div>
          ) : domains.length === 0 ? (
            <div style={{ textAlign: 'center', padding: '40px 0', color: '#64748b' }}>
              No domains found for this industry.
            </div>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
              {domains.map(d => {
                const isDomExpanded = !!expandedDomains[d.domain_name];
                const entities = d.entities || [];
                const filteredEntities = entities.filter(ent => {
                  if (!q) return true;
                  if (d.domain_name.toLowerCase().includes(q)) return true;
                  if (ent.table_name.toLowerCase().includes(q)) return true;
                  return (ent.columns || []).some(col => col.column_name.toLowerCase().includes(q));
                });

                if (q && filteredEntities.length === 0) return null;

                return (
                  <div key={d.domain_name} style={{
                    background: '#ffffff',
                    border: '1px solid #e2e8f0',
                    borderRadius: 8,
                    overflow: 'hidden',
                    boxShadow: '0 1px 3px rgba(0,0,0,0.03)',
                  }}>
                    {/* Domain Header */}
                    <div
                      onClick={() => toggleDomain(d.domain_name)}
                      style={{
                        padding: '10px 14px',
                        background: '#f8fafc',
                        borderBottom: isDomExpanded ? '1px solid #e2e8f0' : 'none',
                        display: 'flex',
                        alignItems: 'center',
                        justifyContent: 'space-between',
                        cursor: 'pointer',
                        userSelect: 'none',
                      }}
                    >
                      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                        <span style={{ fontSize: 13, color: '#7500C0', transform: isDomExpanded ? 'rotate(90deg)' : 'none', display: 'inline-block', transition: 'transform 0.15s ease' }}>
                          ▶
                        </span>
                        <div>
                          <strong style={{ fontSize: 13, color: '#0f172a' }}>{d.domain_name}</strong>
                          <span style={{ fontSize: 11, color: '#64748b', marginLeft: 8 }}>
                            {entities.length} tables · {entities.reduce((acc, e) => acc + (e.columns || []).length, 0)} canonical columns
                          </span>
                        </div>
                      </div>
                      <span style={{ fontSize: 10, background: '#ede9fe', color: '#6b21a8', padding: '2px 8px', borderRadius: 10, fontWeight: 700 }}>
                        DOMAIN
                      </span>
                    </div>

                    {/* Tables Accordion */}
                    {isDomExpanded && (
                      <div style={{ padding: '10px 14px', display: 'flex', flexDirection: 'column', gap: 10 }}>
                        {filteredEntities.map(e => {
                          const tableKey = `${d.domain_name}::${e.table_name}`;
                          const isTableExpanded = !!expandedTables[tableKey];
                          const cols = e.columns || [];
                          const selectedCols = selectedAttributes[e.table_name] || new Set();
                          const isAllColsSelected = cols.length > 0 && cols.every(c => selectedCols.has(c.column_name));
                          const isSomeColsSelected = cols.some(c => selectedCols.has(c.column_name)) && !isAllColsSelected;

                          return (
                            <div key={e.table_name} style={{
                              border: '1px solid #e2e8f0',
                              borderRadius: 6,
                              background: '#ffffff',
                              overflow: 'hidden',
                            }}>
                              {/* Table Header */}
                              <div style={{
                                padding: '8px 12px',
                                background: '#fdfdfd',
                                borderBottom: isTableExpanded ? '1px solid #f1f5f9' : 'none',
                                display: 'flex',
                                alignItems: 'center',
                                justifyContent: 'space-between',
                              }}>
                                <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                                  <input
                                    type="checkbox"
                                    checked={isAllColsSelected}
                                    ref={input => { if (input) input.indeterminate = isSomeColsSelected; }}
                                    onChange={() => handleTableToggleAll(e.table_name, cols)}
                                    style={{ cursor: 'pointer', accentColor: '#7500C0' }}
                                  />
                                  <span
                                    onClick={() => toggleTable(tableKey)}
                                    style={{
                                      fontFamily: 'monospace',
                                      fontSize: 12,
                                      fontWeight: 700,
                                      color: '#0f172a',
                                      cursor: 'pointer',
                                    }}
                                  >
                                    {e.table_name}
                                  </span>
                                  <span style={{ fontSize: 10, color: '#64748b' }}>
                                    ({selectedCols.size}/{cols.length} cols)
                                  </span>
                                </div>
                                <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                                  <span style={{ fontSize: 10, color: '#64748b', maxWidth: 300, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                                    {e.description}
                                  </span>
                                  <button
                                    type="button"
                                    onClick={() => toggleTable(tableKey)}
                                    style={{
                                      background: 'none',
                                      border: 'none',
                                      fontSize: 11,
                                      color: '#7500C0',
                                      cursor: 'pointer',
                                      fontWeight: 600,
                                    }}
                                  >
                                    {isTableExpanded ? '▲ Hide' : '▼ View Columns'}
                                  </button>
                                </div>
                              </div>

                              {/* Attributes Checklist */}
                              {isTableExpanded && (
                                <div style={{
                                  padding: '8px 12px',
                                  display: 'grid',
                                  gridTemplateColumns: 'repeat(auto-fill, minmax(260px, 1fr))',
                                  gap: 6,
                                  background: '#fafbfc',
                                }}>
                                  {cols.map(c => {
                                    const isChecked = selectedCols.has(c.column_name);
                                    return (
                                      <label
                                        key={c.column_name}
                                        style={{
                                          display: 'flex',
                                          alignItems: 'center',
                                          gap: 6,
                                          padding: '4px 6px',
                                          borderRadius: 4,
                                          background: isChecked ? '#ede9fe' : '#ffffff',
                                          border: `1px solid ${isChecked ? '#c4b5fd' : '#e2e8f0'}`,
                                          cursor: 'pointer',
                                          fontSize: 11,
                                        }}
                                      >
                                        <input
                                          type="checkbox"
                                          checked={isChecked}
                                          onChange={() => handleAttributeToggle(e.table_name, c.column_name)}
                                          style={{ accentColor: '#7500C0' }}
                                        />
                                        <span style={{ fontFamily: 'monospace', fontWeight: 600, color: '#1e293b', flex: 1 }}>
                                          {c.column_name}
                                        </span>
                                        <span style={{
                                          fontSize: 9,
                                          background: '#f1f5f9',
                                          color: '#475569',
                                          padding: '1px 4px',
                                          borderRadius: 3,
                                          fontFamily: 'monospace',
                                        }}>
                                          {c.data_type}
                                        </span>
                                      </label>
                                    );
                                  })}
                                </div>
                              )}
                            </div>
                          );
                        })}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </div>

        {/* Footer / Action Bar */}
        <div style={{
          padding: '12px 24px',
          background: '#ffffff',
          borderTop: '1px solid #e2e8f0',
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
        }}>
          <div>
            <span style={{ fontSize: 12, fontWeight: 700, color: '#1e293b' }}>
              Selected: <span style={{ color: '#7500C0' }}>{totalSelectedTables}</span> tables · <span style={{ color: '#7500C0' }}>{totalSelectedAttributes}</span> attributes
            </span>
            <div style={{ fontSize: 10.5, color: '#64748b' }}>
              Applying will formulate a structured schema augmentation directive in your chat session.
            </div>
          </div>

          <div style={{ display: 'flex', gap: 10 }}>
            <button
              type="button"
              onClick={onClose}
              style={{
                background: '#ffffff',
                border: '1px solid #cbd5e1',
                borderRadius: 6,
                padding: '7px 16px',
                fontSize: 12,
                fontWeight: 600,
                color: '#475569',
                cursor: 'pointer',
              }}
            >
              Cancel
            </button>
            <button
              type="button"
              disabled={totalSelectedAttributes === 0}
              onClick={handleApplySelection}
              style={{
                background: totalSelectedAttributes > 0 ? '#7500C0' : '#cbd5e1',
                border: 'none',
                borderRadius: 6,
                padding: '7px 18px',
                fontSize: 12,
                fontWeight: 700,
                color: '#ffffff',
                cursor: totalSelectedAttributes > 0 ? 'pointer' : 'not-allowed',
                boxShadow: totalSelectedAttributes > 0 ? '0 2px 4px rgba(117,0,192,0.3)' : 'none',
              }}
            >
              Apply Selection to Requirement
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
