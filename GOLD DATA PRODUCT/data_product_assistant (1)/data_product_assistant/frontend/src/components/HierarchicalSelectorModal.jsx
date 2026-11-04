import React, { useState, useEffect, useMemo } from 'react';
import { fetchIndustries, fetchIndustryDomains, submitNavigatorSelection } from '../api/catalog';

export default function HierarchicalSelectorModal({ isOpen, sessionId, onClose, onApply, onOpenExistingStructure }) {
  const [industries, setIndustries] = useState([]);
  const [selectedIndustryId, setSelectedIndustryId] = useState('banking');
  const [domains, setDomains] = useState([]);
  const [activeDomainFilter, setActiveDomainFilter] = useState('ALL');
  const [loading, setLoading] = useState(false);
  const [searchQuery, setSearchQuery] = useState('');

  // Selection state: map of table -> Set of attribute names
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
          setActiveDomainFilter('ALL');
          const expDom = {};
          const expTab = {};
          data.domains.forEach(d => {
            const dName = d.domain_name || d.display_name || d.name;
            expDom[dName] = true;
            const entList = Array.isArray(d.entities) ? d.entities : (d.entities_list || []);
            entList.forEach(e => {
              expTab[`${dName}::${e.table_name}`] = false;
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
      const allSelected = allCols.length > 0 && allCols.every(c => current.has(c.column_name || c.name));
      if (allSelected) {
        const next = { ...prev };
        delete next[tableName];
        return next;
      } else {
        return { ...prev, [tableName]: new Set(allCols.map(c => c.column_name || c.name)) };
      }
    });
  };

  const handleSelectOnlySingleEntity = (tableName, allCols) => {
    setSelectedAttributes({
      [tableName]: new Set(allCols.map(c => c.column_name || c.name)),
    });
  };

  const handleSelectAllInDomain = (domainObj) => {
    const entList = Array.isArray(domainObj.entities) ? domainObj.entities : (domainObj.entities_list || []);
    setSelectedAttributes(prev => {
      const next = { ...prev };
      entList.forEach(e => {
        next[e.table_name] = new Set((e.columns || []).map(c => c.column_name || c.name));
      });
      return next;
    });
  };

  const handleSelectAllInIndustry = () => {
    const next = {};
    domains.forEach(d => {
      const entList = Array.isArray(d.entities) ? d.entities : (d.entities_list || []);
      entList.forEach(e => {
        next[e.table_name] = new Set((e.columns || []).map(c => c.column_name || c.name));
      });
    });
    setSelectedAttributes(next);
  };

  const handleClearSelection = () => {
    setSelectedAttributes({});
  };

  const selectedTableNames = useMemo(() => {
    return Object.entries(selectedAttributes)
      .filter(([, s]) => s && s.size > 0)
      .map(([tbl]) => tbl);
  }, [selectedAttributes]);

  const totalSelectedAttributes = useMemo(() => {
    return Object.values(selectedAttributes).reduce((acc, s) => acc + (s ? s.size : 0), 0);
  }, [selectedAttributes]);

  const totalSelectedTables = selectedTableNames.length;

  const activeDomainName = useMemo(() => {
    if (activeDomainFilter !== 'ALL') return activeDomainFilter;
    // Find which domain contains the first selected table
    for (const d of domains) {
      const dName = d.domain_name || d.display_name || d.name;
      const entList = Array.isArray(d.entities) ? d.entities : (d.entities_list || []);
      if (entList.some(e => selectedTableNames.includes(e.table_name))) {
        return dName;
      }
    }
    return domains[0]?.domain_name || domains[0]?.display_name || 'Core Banking & Accounts';
  }, [activeDomainFilter, domains, selectedTableNames]);

  const buildPlainAttributesMap = () => {
    const out = {};
    Object.entries(selectedAttributes).forEach(([tbl, colsSet]) => {
      if (colsSet && colsSet.size > 0) {
        out[tbl] = Array.from(colsSet);
      }
    });
    return out;
  };

  const handleApplySelection = async () => {
    const currentIndustry = industries.find(i => i.id === selectedIndustryId);
    const plainAttrs = buildPlainAttributesMap();
    const lines = [];
    lines.push(`Augment Conformed Gold Model for Industry: **${currentIndustry ? currentIndustry.name : selectedIndustryId}** — Domain: **${activeDomainName}**`);
    Object.entries(plainAttrs).forEach(([tbl, colsArr]) => {
      lines.push(`- **Table \`${tbl}\`**: ${colsArr.map(c => `\`${c}\``).join(', ')}`);
    });

    try {
      await submitNavigatorSelection({
        session_id: sessionId,
        industry_id: selectedIndustryId,
        domain_name: activeDomainName,
        selection_mode: selectedTableNames.length === 1 ? 'single' : 'multiple',
        selected_entities: selectedTableNames,
        selected_attributes: plainAttrs,
      });
    } catch (err) {
      console.warn('Navigator backend sync warning:', err);
    }

    const formattedPrompt = lines.join('\n');
    onApply(formattedPrompt, {
      action: 'navigator_apply',
      industryId: selectedIndustryId,
      domainName: activeDomainName,
      selectedEntities: selectedTableNames,
      selectedAttributes: plainAttrs,
      table_count: totalSelectedTables,
      attribute_count: totalSelectedAttributes,
    });
    onClose();
  };

  const handleOpenExisting = () => {
    const plainAttrs = buildPlainAttributesMap();
    if (onOpenExistingStructure) {
      onOpenExistingStructure({
        industryId: selectedIndustryId,
        domainName: activeDomainName,
        selectedEntities: selectedTableNames,
        selectedAttributes: plainAttrs,
      });
    }
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
        maxWidth: 1020,
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
                Enterprise Domain &amp; Gold Schema Navigator
              </h3>
              <span style={{
                background: 'rgba(255,255,255,0.2)',
                fontSize: 10.5,
                fontWeight: 600,
                padding: '2px 8px',
                borderRadius: 12,
              }}>
                Industry → Domain → Entities (All / Single / Multi) → Attributes
              </span>
            </div>
            <p style={{ margin: '4px 0 0', fontSize: 11.5, opacity: 0.9 }}>
              Navigate available domains across industries, select all entities, a single entity, or multiple entities, or open an existing Gold structure to edit.
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
              }}
            >
              <span>{ind.icon || '🏷️'}</span>
              <span>{ind.name}</span>
            </button>
          ))}
        </div>

        {/* Domain Navigation Bar */}
        <div style={{
          padding: '8px 24px',
          background: '#faf5ff',
          borderBottom: '1px solid #e9d5ff',
          display: 'flex',
          alignItems: 'center',
          gap: 6,
          overflowX: 'auto',
        }}>
          <span style={{ fontSize: 10.5, fontWeight: 700, color: '#6b21a8', textTransform: 'uppercase', whiteSpace: 'nowrap' }}>
            Available Domains:
          </span>
          <button
            type="button"
            onClick={() => setActiveDomainFilter('ALL')}
            style={{
              background: activeDomainFilter === 'ALL' ? '#460073' : '#ffffff',
              color: activeDomainFilter === 'ALL' ? '#ffffff' : '#460073',
              border: '1px solid #d8b4fe',
              borderRadius: 14,
              padding: '3px 10px',
              fontSize: 11,
              fontWeight: 700,
              cursor: 'pointer',
              whiteSpace: 'nowrap',
            }}
          >
            All Domains ({domains.length})
          </button>
          {domains.map(d => {
            const dName = d.domain_name || d.display_name || d.name;
            const entCount = Array.isArray(d.entities) ? d.entities.length : (d.entities_list?.length || d.entity_count || 0);
            const isActive = activeDomainFilter === dName;
            return (
              <button
                key={dName}
                type="button"
                onClick={() => setActiveDomainFilter(dName)}
                style={{
                  background: isActive ? '#7500C0' : '#ffffff',
                  color: isActive ? '#ffffff' : '#334155',
                  border: `1px solid ${isActive ? '#7500C0' : '#cbd5e1'}`,
                  borderRadius: 14,
                  padding: '3px 10px',
                  fontSize: 11,
                  fontWeight: isActive ? 700 : 500,
                  cursor: 'pointer',
                  whiteSpace: 'nowrap',
                }}
              >
                {dName} ({entCount})
              </button>
            );
          })}
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
              placeholder="Search domains, tables (e.g. slv_account), or column names..."
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
                const dName = d.domain_name || d.display_name || d.name;
                if (activeDomainFilter !== 'ALL' && activeDomainFilter !== dName) return null;

                const isDomExpanded = !!expandedDomains[dName];
                const entities = Array.isArray(d.entities) ? d.entities : (d.entities_list || []);
                const filteredEntities = entities.filter(ent => {
                  if (!q) return true;
                  if (dName.toLowerCase().includes(q)) return true;
                  if (ent.table_name.toLowerCase().includes(q)) return true;
                  if ((ent.description || '').toLowerCase().includes(q)) return true;
                  return (ent.columns || []).some(col => (col.column_name || col.name || '').toLowerCase().includes(q));
                });

                if (q && filteredEntities.length === 0) return null;

                return (
                  <div key={dName} style={{
                    background: '#ffffff',
                    border: '1px solid #e2e8f0',
                    borderRadius: 8,
                    overflow: 'hidden',
                    boxShadow: '0 1px 3px rgba(0,0,0,0.03)',
                  }}>
                    {/* Domain Header */}
                    <div
                      style={{
                        padding: '10px 14px',
                        background: '#f8fafc',
                        borderBottom: isDomExpanded ? '1px solid #e2e8f0' : 'none',
                        display: 'flex',
                        alignItems: 'center',
                        justifyContent: 'space-between',
                        userSelect: 'none',
                        flexWrap: 'wrap',
                        gap: 8,
                      }}
                    >
                      <div
                        onClick={() => toggleDomain(dName)}
                        style={{ display: 'flex', alignItems: 'center', gap: 8, cursor: 'pointer', flex: 1 }}
                      >
                        <span style={{ fontSize: 13, color: '#7500C0', transform: isDomExpanded ? 'rotate(90deg)' : 'none', display: 'inline-block', transition: 'transform 0.15s ease' }}>
                          ▶
                        </span>
                        <div>
                          <strong style={{ fontSize: 13, color: '#0f172a' }}>{dName}</strong>
                          <span style={{ fontSize: 11, color: '#64748b', marginLeft: 8 }}>
                            {entities.length} canonical entities · {entities.reduce((acc, e) => acc + (e.columns || []).length, 0)} columns
                          </span>
                          {d.description && (
                            <div style={{ fontSize: 11, color: '#64748b', marginTop: 2 }}>{d.description}</div>
                          )}
                        </div>
                      </div>
                      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                        <button
                          type="button"
                          onClick={() => handleSelectAllInDomain(d)}
                          style={{
                            fontSize: 10.5,
                            background: '#f3e8ff',
                            color: '#6b21a8',
                            border: '1px solid #d8b4fe',
                            padding: '3px 9px',
                            borderRadius: 6,
                            fontWeight: 700,
                            cursor: 'pointer',
                          }}
                        >
                          ✓ Select All {entities.length} Entities in Domain
                        </button>
                        <span style={{ fontSize: 10, background: '#ede9fe', color: '#6b21a8', padding: '2px 8px', borderRadius: 10, fontWeight: 700 }}>
                          DOMAIN
                        </span>
                      </div>
                    </div>

                    {/* Tables Accordion */}
                    {isDomExpanded && (
                      <div style={{ padding: '10px 14px', display: 'flex', flexDirection: 'column', gap: 10 }}>
                        {filteredEntities.map(e => {
                          const tableKey = `${dName}::${e.table_name}`;
                          const isTableExpanded = !!expandedTables[tableKey];
                          const cols = e.columns || [];
                          const selectedCols = selectedAttributes[e.table_name] || new Set();
                          const isAllColsSelected = cols.length > 0 && cols.every(c => selectedCols.has(c.column_name || c.name));
                          const isSomeColsSelected = cols.some(c => selectedCols.has(c.column_name || c.name)) && !isAllColsSelected;
                          const isOnlyThisEntitySelected = totalSelectedTables === 1 && selectedTableNames[0] === e.table_name;

                          return (
                            <div key={e.table_name} style={{
                              border: (isAllColsSelected || isSomeColsSelected) ? '1.5px solid #7500C0' : '1px solid #e2e8f0',
                              borderRadius: 6,
                              background: '#ffffff',
                              overflow: 'hidden',
                            }}>
                              {/* Table Header */}
                              <div style={{
                                padding: '8px 12px',
                                background: (isAllColsSelected || isSomeColsSelected) ? '#fcfaff' : '#fdfdfd',
                                borderBottom: isTableExpanded ? '1px solid #f1f5f9' : 'none',
                                display: 'flex',
                                alignItems: 'center',
                                justifyContent: 'space-between',
                                flexWrap: 'wrap',
                                gap: 8,
                              }}>
                                <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                                  <input
                                    type="checkbox"
                                    checked={isAllColsSelected}
                                    ref={input => { if (input) input.indeterminate = isSomeColsSelected; }}
                                    onChange={() => handleTableToggleAll(e.table_name, cols)}
                                    style={{ cursor: 'pointer', accentColor: '#7500C0', width: 15, height: 15 }}
                                  />
                                  <span
                                    onClick={() => toggleTable(tableKey)}
                                    style={{
                                      fontFamily: 'monospace',
                                      fontSize: 12.5,
                                      fontWeight: 700,
                                      color: '#0f172a',
                                      cursor: 'pointer',
                                    }}
                                  >
                                    {e.table_name}
                                  </span>
                                  <span style={{
                                    fontSize: 9.5,
                                    fontWeight: 700,
                                    padding: '1px 6px',
                                    borderRadius: 4,
                                    background: '#eff6ff',
                                    color: '#1d4ed8',
                                    textTransform: 'uppercase',
                                  }}>
                                    {e.type || e.entity_type || 'dimension'}
                                  </span>
                                  <span style={{ fontSize: 10.5, color: '#64748b' }}>
                                    ({selectedCols.size}/{cols.length} cols)
                                  </span>
                                </div>
                                <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                                  <span style={{ fontSize: 10.5, color: '#64748b', maxWidth: 280, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                                    {e.description}
                                  </span>
                                  <button
                                    type="button"
                                    onClick={() => handleSelectOnlySingleEntity(e.table_name, cols)}
                                    title="Select ONLY this specific entity"
                                    style={{
                                      background: isOnlyThisEntitySelected ? '#7500C0' : '#f3e8ff',
                                      color: isOnlyThisEntitySelected ? '#ffffff' : '#6b21a8',
                                      border: '1px solid #d8b4fe',
                                      borderRadius: 5,
                                      padding: '2px 8px',
                                      fontSize: 10.5,
                                      fontWeight: 700,
                                      cursor: 'pointer',
                                    }}
                                  >
                                    ◉ Only This Entity
                                  </button>
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
                                    {isTableExpanded ? '▲ Hide Columns' : '▼ View Columns'}
                                  </button>
                                </div>
                              </div>

                              {/* Attributes Checklist */}
                              {isTableExpanded && (
                                <div style={{
                                  padding: '8px 12px',
                                  display: 'grid',
                                  gridTemplateColumns: 'repeat(auto-fill, minmax(250px, 1fr))',
                                  gap: 6,
                                  background: '#fafbfc',
                                }}>
                                  {cols.map(c => {
                                    const colName = c.column_name || c.name;
                                    const isChecked = selectedCols.has(colName);
                                    return (
                                      <label
                                        key={colName}
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
                                          onChange={() => handleAttributeToggle(e.table_name, colName)}
                                          style={{ accentColor: '#7500C0' }}
                                        />
                                        <span style={{ fontFamily: 'monospace', fontWeight: 600, color: '#1e293b', flex: 1 }}>
                                          {colName}
                                        </span>
                                        {(c.is_pk || c.is_primary_key) && (
                                          <span style={{ fontSize: 8.5, background: '#fef3c7', color: '#b45309', padding: '0 4px', borderRadius: 3, fontWeight: 700 }}>PK</span>
                                        )}
                                        <span style={{
                                          fontSize: 9,
                                          background: '#f1f5f9',
                                          color: '#475569',
                                          padding: '1px 4px',
                                          borderRadius: 3,
                                          fontFamily: 'monospace',
                                        }}>
                                          {c.data_type || c.type}
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
          flexWrap: 'wrap',
          gap: 10,
        }}>
          <div>
            <span style={{ fontSize: 12, fontWeight: 700, color: '#1e293b' }}>
              Selected Scope:{' '}
              <span style={{ color: '#7500C0' }}>{totalSelectedTables}</span>{' '}
              {totalSelectedTables === 1 ? 'entity' : 'entities'} ·{' '}
              <span style={{ color: '#7500C0' }}>{totalSelectedAttributes}</span> attributes
            </span>
            <div style={{ fontSize: 10.5, color: '#64748b' }}>
              {totalSelectedTables === 1
                ? `Single-entity mode: only ${selectedTableNames[0]} will be generated.`
                : 'Select 1 entity, multiple entities, or all entities in a domain.'}
            </div>
          </div>

          <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap' }}>
            <button
              type="button"
              onClick={onClose}
              style={{
                background: '#ffffff',
                border: '1px solid #cbd5e1',
                borderRadius: 6,
                padding: '7px 14px',
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
              onClick={handleOpenExisting}
              style={{
                background: '#f3e8ff',
                border: '1px solid #7500C0',
                borderRadius: 6,
                padding: '7px 14px',
                fontSize: 12,
                fontWeight: 700,
                color: '#460073',
                cursor: 'pointer',
              }}
            >
              🛠 Open &amp; Edit Existing Gold structure
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
              Apply Scope ({totalSelectedTables} {totalSelectedTables === 1 ? 'Entity' : 'Entities'})
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

