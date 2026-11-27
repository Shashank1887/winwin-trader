import { StatusBar } from "expo-status-bar";
import React, { useCallback, useEffect, useState } from "react";
import {
  ActivityIndicator,
  Alert,
  Pressable,
  RefreshControl,
  ScrollView,
  StyleSheet,
  Switch,
  Text,
  TextInput,
  View,
  useWindowDimensions,
} from "react-native";
import { SafeAreaProvider, SafeAreaView } from "react-native-safe-area-context";

import {
  analyzeFno,
  configureAutomation,
  connectAngelBroker,
  disconnectAngelBroker,
  executeFnoTrade,
  executeSignalTrade,
  fetchFnoState,
  fetchState,
  loadServerConfig,
  panicSquareOffAll,
  panicSquareOffFno,
  refreshLtps,
  saveServerConfig,
  scanMarket,
} from "./src/api";
import {
  AppStateResponse,
  FnoContract,
  FnoStateResponse,
  IndexTrend,
  ServerConfig,
  TradingMode,
} from "./src/types";

type TabKey = "signals" | "trades" | "fno" | "settings";

function formatINR(val: number, signed = false): string {
  const abs = Math.abs(val).toLocaleString("en-IN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
  if (val < 0) return `-₹${abs}`;
  if (signed && val > 0) return `+₹${abs}`;
  return `₹${abs}`;
}

export default function App() {
  return (
    <SafeAreaProvider>
      <MainScreen />
    </SafeAreaProvider>
  );
}

function MainScreen() {
  const { width } = useWindowDimensions();
  const isTablet = width >= 700;

  const [tab, setTab] = useState<TabKey>("signals");
  const [paperMode, setPaperMode] = useState<boolean>(true);
  const [autoExecute, setAutoExecute] = useState<boolean>(false);
  const [scanIntervalSec, setScanIntervalSec] = useState<number>(60);
  const [autoEodExit, setAutoEodExit] = useState<boolean>(false);
  const [showDiagnostics, setShowDiagnostics] = useState<boolean>(false);

  const [config, setConfig] = useState<ServerConfig>({
    baseUrl: "",
    apiToken: "",
    capital: "50000",
    maxSl: "400",
    maxTrades: "2",
    windowPreset: "morning",
    universePreset: "top15",
    customSymbols: "RELIANCE, TCS, TRENT, BEL, HAL",
    trailingSlEnabled: true,
    niftyTrendFilter: true,
  });
  const [fnoUnderlying, setFnoUnderlying] = useState<"NIFTY" | "BANKNIFTY" | "FINNIFTY">("NIFTY");
  const [fnoInstrument, setFnoInstrument] = useState<"OPTIDX" | "FUTIDX">("OPTIDX");
  const [fnoSlPoints, setFnoSlPoints] = useState("15");
  const [fnoLots, setFnoLots] = useState("1");
  const [fnoEntryPx, setFnoEntryPx] = useState("100");
  const [fnoAck, setFnoAck] = useState(false);
  const [fnoTrend, setFnoTrend] = useState<IndexTrend | null>(null);
  const [fnoContracts, setFnoContracts] = useState<FnoContract[]>([]);
  const [fnoSelectedTsym, setFnoSelectedTsym] = useState("");
  const [fnoState, setFnoState] = useState<FnoStateResponse | null>(null);
  const [apiKey, setApiKey] = useState("");
  const [clientId, setClientId] = useState("");
  const [password, setPassword] = useState("");
  const [totpSecret, setTotpSecret] = useState("");

  const [appState, setAppState] = useState<AppStateResponse | null>(null);
  const [loading, setLoading] = useState<boolean>(false);
  const [refreshing, setRefreshing] = useState<boolean>(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [noticeMsg, setNoticeMsg] = useState<string | null>(null);

  const mode: TradingMode = paperMode ? "PAPER" : "LIVE";

  const syncState = useCallback(
    async (cfgOverride?: ServerConfig, modeOverride?: TradingMode) => {
      const activeCfg = cfgOverride || config;
      const activeMode = modeOverride || mode;
      if (!activeCfg.baseUrl || !activeCfg.apiToken) return;
      try {
        setErrorMsg(null);
        const data = await fetchState(activeCfg, activeMode);
        setAppState(data);
        if (data.notice) setNoticeMsg(data.notice);
      } catch (err: any) {
        setErrorMsg(err?.message || "Failed to connect to backend.");
      }
    },
    [config, mode]
  );

  useEffect(() => {
    loadServerConfig().then((loaded) => {
      setConfig(loaded);
      if (loaded.baseUrl && loaded.apiToken) {
        syncState(loaded, "PAPER");
      } else {
        setTab("settings");
      }
    });
  }, []);

  useEffect(() => {
    if (!config.baseUrl || !config.apiToken || !appState?.automation?.enabled) return;
    const timer = setInterval(() => {
      syncState();
    }, 10000);
    return () => clearInterval(timer);
  }, [config.baseUrl, config.apiToken, appState?.automation?.enabled, syncState]);

  const handleToggleAutoPilot = async () => {
    const nextEnabled = !(appState?.automation?.enabled ?? false);
    if (nextEnabled && autoExecute && !paperMode) {
      Alert.alert(
        "Start LIVE Auto-Pilot?",
        "Auto-Execute is ON in LIVE mode. Real orders will place automatically inside 09:20–11:30 IST.",
        [
          { text: "Cancel", style: "cancel" },
          {
            text: "Start LIVE Auto-Pilot",
            style: "destructive",
            onPress: async () => {
              const next = await configureAutomation(config, {
                enabled: true,
                mode,
                scanIntervalSec,
                ltpIntervalSec: 15,
                autoExecute,
                autoSquareOffEod: autoEodExit,
              });
              setAppState(next);
              setNoticeMsg(next.notice || "Auto-Pilot started.");
            },
          },
        ]
      );
      return;
    }
    setLoading(true);
    setErrorMsg(null);
    try {
      const next = await configureAutomation(config, {
        enabled: nextEnabled,
        mode,
        scanIntervalSec,
        ltpIntervalSec: 15,
        autoExecute,
        autoSquareOffEod: autoEodExit,
      });
      setAppState(next);
      setNoticeMsg(next.notice || "Auto-Pilot updated.");
    } catch (err: any) {
      setErrorMsg(err?.message || "Failed to update Auto-Pilot.");
    } finally {
      setLoading(false);
    }
  };

  const handleToggleMode = (nextPaper: boolean) => {
    if (!nextPaper) {
      Alert.alert(
        "Switch to LIVE Trading?",
        "Orders in LIVE mode execute real money trades on your Angel One account.",
        [
          { text: "Stay in Paper", style: "cancel" },
          {
            text: "Enable LIVE",
            style: "destructive",
            onPress: () => {
              setPaperMode(false);
              syncState(config, "LIVE");
            },
          },
        ]
      );
      return;
    }
    setPaperMode(true);
    syncState(config, "PAPER");
  };

  const handlePullRefresh = async () => {
    setRefreshing(true);
    await syncState();
    setRefreshing(false);
  };

  const handleScan = async (previewOutsideWindow: boolean) => {
    setLoading(true);
    setErrorMsg(null);
    try {
      const next = await scanMarket(config, mode, previewOutsideWindow, autoExecute);
      setAppState(next);
      setNoticeMsg(next.notice || "Scan complete.");
    } catch (err: any) {
      setErrorMsg(err?.message || "Scan failed.");
    } finally {
      setLoading(false);
    }
  };

  const handleExecute = async (symbol: string) => {
    setLoading(true);
    setErrorMsg(null);
    try {
      const next = await executeSignalTrade(config, mode, symbol);
      setAppState(next);
      setNoticeMsg(next.notice || `Executed ${symbol}`);
    } catch (err: any) {
      setErrorMsg(err?.message || `Execution failed for ${symbol}`);
    } finally {
      setLoading(false);
    }
  };

  const handleRefreshLtps = async () => {
    setLoading(true);
    setErrorMsg(null);
    try {
      const next = await refreshLtps(config, mode);
      setAppState(next);
      setNoticeMsg(next.notice || "LTPs refreshed.");
    } catch (err: any) {
      setErrorMsg(err?.message || "Failed to refresh LTPs.");
    } finally {
      setLoading(false);
    }
  };

  const handlePanicExit = () => {
    Alert.alert(
      "Square Off All Positions?",
      `This will immediately exit all OPEN ${mode} positions at market price.`,
      [
        { text: "Cancel", style: "cancel" },
        {
          text: "SQUARE OFF ALL",
          style: "destructive",
          onPress: async () => {
            setLoading(true);
            setErrorMsg(null);
            try {
              const next = await panicSquareOffAll(config, mode);
              setAppState(next);
              setNoticeMsg(next.notice || "Panic square-off executed.");
            } catch (err: any) {
              setErrorMsg(err?.message || "Panic exit failed.");
            } finally {
              setLoading(false);
            }
          },
        },
      ]
    );
  };

  const handleSaveServer = async () => {
    setLoading(true);
    setErrorMsg(null);
    try {
      const clampedSl = Math.min(400, Math.max(50, Number(config.maxSl || 400)));
      const clampedTrades = Math.min(2, Math.max(1, Number(config.maxTrades || 2)));
      const nextCfg: ServerConfig = {
        ...config,
        maxSl: String(clampedSl),
        maxTrades: String(clampedTrades),
      };
      setConfig(nextCfg);
      await saveServerConfig(nextCfg);
      const data = await fetchState(nextCfg, mode);
      setAppState(data);
      setNoticeMsg("Connected to HTTPS backend and saved securely.");
      setTab("signals");
    } catch (err: any) {
      setErrorMsg(err?.message || "Could not connect to backend.");
    } finally {
      setLoading(false);
    }
  };

  const handleConnectBroker = async () => {
    setLoading(true);
    setErrorMsg(null);
    try {
      const res = await connectAngelBroker(config, {
        apiKey,
        clientId,
        password,
        totpSecret,
      });
      setNoticeMsg(res.message);
      await syncState();
    } catch (err: any) {
      setErrorMsg(err?.message || "SmartAPI login failed.");
    } finally {
      setLoading(false);
    }
  };

  const handleDisconnectBroker = async () => {
    setLoading(true);
    try {
      const res = await disconnectAngelBroker(config);
      setNoticeMsg(res.message);
      await syncState();
    } catch (err: any) {
      setErrorMsg(err?.message || "Failed to disconnect.");
    } finally {
      setLoading(false);
    }
  };

  const handleAnalyzeFno = async () => {
    setLoading(true);
    setErrorMsg(null);
    try {
      const res = await analyzeFno(config, mode, fnoUnderlying, fnoInstrument);
      setFnoTrend(res.trend);
      setFnoContracts(res.contracts || []);
      if (res.contracts?.[0]) {
        setFnoSelectedTsym(res.contracts[0].tradingsymbol);
        if (res.contracts[0].ltp && res.contracts[0].ltp > 0) {
          setFnoEntryPx(String(res.contracts[0].ltp));
        }
      }
      const fs = await fetchFnoState(config, mode);
      setFnoState(fs);
    } catch (err: any) {
      setErrorMsg(err?.message || "F&O analysis failed.");
    } finally {
      setLoading(false);
    }
  };

  const handleExecuteFno = async () => {
    const chosen = fnoContracts.find((c) => c.tradingsymbol === fnoSelectedTsym);
    if (!chosen) {
      setErrorMsg("Analyze spot & select an NFO contract first.");
      return;
    }
    setLoading(true);
    setErrorMsg(null);
    try {
      const lots = Math.min(2, Math.max(1, Number(fnoLots || 1)));
      const fs = await executeFnoTrade(config, {
        mode,
        tradingsymbol: chosen.tradingsymbol,
        token: String(chosen.token),
        qty: Number(chosen.lotsize) * lots,
        entryPrice: Number(fnoEntryPx || 100),
        slPoints: Number(fnoSlPoints || 15),
        segment: fnoInstrument === "OPTIDX" ? "NFO_OPTIONS" : "NFO_FUTURES",
        riskAck: fnoAck,
      });
      setFnoState(fs);
      setNoticeMsg(fs.notice || "F&O trade executed.");
    } catch (err: any) {
      setErrorMsg(err?.message || "F&O execution failed.");
    } finally {
      setLoading(false);
    }
  };

  const handlePanicFno = () => {
    Alert.alert("Square Off All F&O?", `Exit all OPEN ${mode} F&O positions at market price?`, [
      { text: "Cancel", style: "cancel" },
      {
        text: "SQUARE OFF F&O",
        style: "destructive",
        onPress: async () => {
          setLoading(true);
          try {
            const fs = await panicSquareOffFno(config, mode);
            setFnoState(fs);
            setNoticeMsg(fs.notice || "F&O square-off completed.");
          } catch (err: any) {
            setErrorMsg(err?.message || "F&O square-off failed.");
          } finally {
            setLoading(false);
          }
        },
      },
    ]);
  };

  const snap = appState?.risk_snapshot;
  const netPnl = snap?.net_pnl ?? 0;
  const realizedPnl = snap?.realized_pnl ?? 0;
  const tradesUsed = snap?.trades_used ?? 0;
  const maxTrades = snap?.max_trades ?? 2;
  const sysStatus = snap?.system_status ?? "ACTIVE";
  const openCount = snap?.open_positions_count ?? 0;
  const canTrade = snap?.can_trade ?? false;
  const withinWindow = snap?.within_active_window ?? false;

  return (
    <SafeAreaView style={styles.safe} edges={["top", "left", "right"]}>
      <StatusBar style="dark" />

      {/* Top App Header + Prominent Paper/Live Switch */}
      <View style={styles.header}>
        <View style={{ flex: 1 }}>
          <Text style={styles.title}>WinWin Intraday Trader</Text>
          <Text style={styles.subtitle}>
            NSE 5m • {appState?.guardrails?.active_window || "09:20–11:30 IST"}
            {appState?.index_trend && !appState.index_trend.error
              ? ` • NIFTY ${appState.index_trend.price} (${appState.index_trend.regime})`
              : ""}{" "}
            • {appState?.time_ist || "Offline"}
          </Text>
        </View>
        <View style={[styles.modeBox, paperMode ? styles.modePaper : styles.modeLive]}>
          <Text style={[styles.modeLabel, { color: paperMode ? "#166534" : "#991B1B" }]}>
            {paperMode ? "PAPER ON" : "LIVE API"}
          </Text>
          <Switch
            value={paperMode}
            onValueChange={handleToggleMode}
            trackColor={{ false: "#FCA5A5", true: "#86EFAC" }}
            thumbColor={paperMode ? "#16A34A" : "#DC2626"}
          />
        </View>
      </View>

      <ScrollView
        style={styles.scroll}
        contentContainerStyle={[
          styles.scrollContent,
          isTablet && styles.scrollContentTablet,
        ]}
        refreshControl={
          <RefreshControl refreshing={refreshing} onRefresh={handlePullRefresh} />
        }
      >
        {/* Lock & Notification Banners */}
        {sysStatus === "GOAL LOCKED" && (
          <View style={[styles.banner, styles.bannerSuccess]}>
            <Text style={styles.bannerTextSuccess}>
              Goal Achieved! Realized P&L reached +₹1,000. Trading auto-locked for today.
            </Text>
          </View>
        )}
        {sysStatus === "KILL-SWITCH LOCKED" && (
          <View style={[styles.banner, styles.bannerError]}>
            <Text style={styles.bannerTextError}>
              Kill-Switch Activated! Daily loss limit (-₹800) hit. All new orders locked.
            </Text>
          </View>
        )}
        {appState && !withinWindow && sysStatus === "ACTIVE" && (
          <View style={[styles.banner, styles.bannerWarn]}>
            <Text style={styles.bannerTextWarn}>
              Outside 09:20 AM – 11:30 AM IST active window. Order execution is locked.
            </Text>
          </View>
        )}
        {errorMsg && (
          <View style={[styles.banner, styles.bannerError]}>
            <Text style={styles.bannerTextError}>{errorMsg}</Text>
          </View>
        )}
        {noticeMsg && !errorMsg && (
          <View style={[styles.banner, styles.bannerInfo]}>
            <Text style={styles.bannerTextInfo}>{noticeMsg}</Text>
          </View>
        )}

        {/* 4 Metric Cards (2x2 on iPhone 14 Plus, 4-across on OnePlus Pad Go) */}
        <View style={styles.metricGrid}>
          <View style={[styles.metricCard, isTablet && styles.metricCardTablet]}>
            <Text style={styles.metricLabel}>Today's Net P&L</Text>
            <Text
              style={[
                styles.metricValue,
                { color: netPnl > 0 ? "#16A34A" : netPnl < 0 ? "#DC2626" : "#0F172A" },
              ]}
            >
              {formatINR(netPnl, true)}
            </Text>
            <Text style={styles.metricSub}>Realized {formatINR(realizedPnl, true)}</Text>
          </View>

          <View style={[styles.metricCard, isTablet && styles.metricCardTablet]}>
            <Text style={styles.metricLabel}>Daily Goal Progress</Text>
            <Text style={styles.metricValue}>
              ₹{Math.round(realizedPnl)} / ₹1,000
            </Text>
            <Text style={styles.metricSub}>
              {(snap?.goal_progress_pct ?? 0).toFixed(0)}% of daily target
            </Text>
          </View>

          <View style={[styles.metricCard, isTablet && styles.metricCardTablet]}>
            <Text style={styles.metricLabel}>Trades Used</Text>
            <Text style={styles.metricValue}>
              {tradesUsed} / {maxTrades}
            </Text>
            <Text style={styles.metricSub}>{openCount} open position(s)</Text>
          </View>

          <View style={[styles.metricCard, isTablet && styles.metricCardTablet]}>
            <Text style={styles.metricLabel}>System Status</Text>
            <Text
              style={[
                styles.metricValue,
                {
                  fontSize: 16,
                  color:
                    sysStatus === "GOAL LOCKED"
                      ? "#16A34A"
                      : sysStatus === "KILL-SWITCH LOCKED"
                      ? "#DC2626"
                      : "#0F172A",
                },
              ]}
            >
              {sysStatus}
            </Text>
            <Text style={styles.metricSub} numberOfLines={1}>
              {appState?.broker_connected
                ? `Angel One: ${appState.broker_profile}`
                : "Angel One: Disconnected"}
            </Text>
          </View>
        </View>

        {loading && (
          <View style={styles.loadingRow}>
            <ActivityIndicator size="small" color="#0F172A" />
            <Text style={styles.loadingText}>Syncing with trading server...</Text>
          </View>
        )}

        {/* CONTROLLED AUTO-PILOT CARD */}
        <View style={styles.sectionCard}>
          <View style={styles.tradeHeader}>
            <View style={{ flex: 1 }}>
              <Text style={styles.sectionTitle}>Controlled Auto-Pilot</Text>
              <Text style={styles.caption}>
                Server Status: {appState?.automation?.state || "IDLE"} • Runs even when phone sleeps
              </Text>
            </View>
            <Pressable
              style={[
                styles.btnSmall,
                appState?.automation?.enabled ? styles.btnPanic : styles.btnPrimary,
                loading && styles.btnDisabled,
              ]}
              disabled={loading}
              onPress={handleToggleAutoPilot}
            >
              <Text style={styles.btnPrimaryText}>
                {appState?.automation?.enabled ? "Stop Auto-Pilot" : "Start Auto-Pilot"}
              </Text>
            </Pressable>
          </View>

          <View style={styles.buttonRow}>
            {[30, 60, 180, 300].map((sec) => {
              const selected = scanIntervalSec === sec;
              return (
                <Pressable
                  key={sec}
                  style={[styles.btnSmall, selected && styles.btnPrimary]}
                  onPress={() => setScanIntervalSec(sec)}
                >
                  <Text style={selected ? styles.btnPrimaryText : styles.btnOutlineText}>
                    {sec < 60 ? `${sec}s` : `${sec / 60}m`}
                  </Text>
                </Pressable>
              );
            })}
          </View>

          <View style={styles.switchRow}>
            <Text style={styles.switchLabel}>Auto-Execute matching BUY setups</Text>
            <Switch value={autoExecute} onValueChange={setAutoExecute} />
          </View>
          <View style={styles.switchRow}>
            <Text style={styles.switchLabel}>Auto Square-Off after 11:30 IST</Text>
            <Switch value={autoEodExit} onValueChange={setAutoEodExit} />
          </View>

          {(appState?.automation?.activity_log?.length ?? 0) > 0 && (
            <View style={styles.emptyBox}>
              {appState?.automation?.activity_log.slice(0, 6).map((line, idx) => (
                <Text key={`${idx}-${line}`} style={styles.caption}>
                  {line}
                </Text>
              ))}
            </View>
          )}
        </View>

        {/* TAB 1: ACTIONABLE SIGNALS */}
        {tab === "signals" && (
          <View style={styles.sectionCard}>
            <Text style={styles.sectionTitle}>Actionable Signals</Text>
            <Text style={styles.caption}>
              Last scan: {appState?.last_scan_time || "-"} • Feed: {appState?.data_source || "-"}
            </Text>

            <View style={styles.buttonRow}>
              <Pressable
                style={[styles.btn, styles.btnPrimary, (!canTrade || loading) && styles.btnDisabled]}
                disabled={!canTrade || loading}
                onPress={() => handleScan(false)}
              >
                <Text style={styles.btnPrimaryText}>Scan Nifty 50</Text>
              </Pressable>

              {!withinWindow && (
                <Pressable
                  style={[styles.btn, styles.btnOutline, loading && styles.btnDisabled]}
                  disabled={loading}
                  onPress={() => handleScan(true)}
                >
                  <Text style={styles.btnOutlineText}>Preview (Read-Only)</Text>
                </Pressable>
              )}
            </View>

            <View style={styles.switchRow}>
              <Text style={styles.switchLabel}>Auto-Execute matching signals</Text>
              <Switch
                value={autoExecute}
                disabled={!canTrade}
                onValueChange={setAutoExecute}
              />
            </View>

            {(appState?.signals?.length ?? 0) === 0 ? (
              <View style={styles.emptyBox}>
                <Text style={styles.emptyText}>
                  No Nifty 50 stocks currently match all 3 BUY conditions (Price {">"} VWAP, 9 EMA {">"} 21 EMA crossover, RSI 55–68 & Vol {">"} 1.5x Avg20).
                </Text>
              </View>
            ) : (
              appState?.signals.map((sig) => (
                <View key={sig.symbol} style={styles.signalItem}>
                  <View style={styles.signalHead}>
                    <Text style={styles.signalSymbol}>{sig.symbol}</Text>
                    <Text style={styles.signalBadge}>BUY SETUP • Qty {sig.qty}</Text>
                  </View>
                  <View style={styles.signalMetrics}>
                    <Text style={styles.signalDetail}>Entry: ₹{sig.price.toFixed(2)}</Text>
                    <Text style={styles.signalDetail}>SL: ₹{sig.stop_loss.toFixed(2)}</Text>
                    <Text style={styles.signalDetail}>Target: ₹{sig.target.toFixed(2)}</Text>
                  </View>
                  <Text style={styles.caption}>
                    RSI {sig.rsi_14} • Vol {sig.volume_ratio}x • VWAP ₹{sig.vwap.toFixed(2)}
                  </Text>
                  <Pressable
                    style={[styles.btn, styles.btnExecute, (!canTrade || loading) && styles.btnDisabled]}
                    disabled={!canTrade || loading}
                    onPress={() => handleExecute(sig.symbol)}
                  >
                    <Text style={styles.btnPrimaryText}>Execute Trade ({sig.symbol})</Text>
                  </Pressable>
                </View>
              ))
            )}

            <Pressable
              style={styles.diagToggle}
              onPress={() => setShowDiagnostics((v) => !v)}
            >
              <Text style={styles.diagToggleText}>
                {showDiagnostics ? "Hide" : "Show"} 15-Stock Diagnostics ({appState?.diagnostics?.length ?? 0})
              </Text>
            </Pressable>

            {showDiagnostics &&
              appState?.diagnostics.map((d) => (
                <View key={d.symbol} style={styles.diagRow}>
                  <Text style={styles.diagSym}>{d.symbol}</Text>
                  <Text style={styles.diagMeta}>
                    ₹{d.price.toFixed(2)} | VWAP {d.vwap.toFixed(2)} | RSI {d.rsi_14} | Vol {d.volume_ratio}x
                  </Text>
                </View>
              ))}
          </View>
        )}

        {/* TAB 2: TODAY'S TRADE BOOK & PANIC EXIT */}
        {tab === "trades" && (
          <View style={styles.sectionCard}>
            <View style={styles.tradeHeader}>
              <Text style={styles.sectionTitle}>Today's Trade Book ({mode})</Text>
              <Pressable
                style={[styles.btnSmall, (openCount === 0 || loading) && styles.btnDisabled]}
                disabled={openCount === 0 || loading}
                onPress={handleRefreshLtps}
              >
                <Text style={styles.btnOutlineText}>Refresh LTPs</Text>
              </Pressable>
            </View>

            <Pressable
              style={[
                styles.btn,
                styles.btnPanic,
                (openCount === 0 || loading) && styles.btnDisabled,
              ]}
              disabled={openCount === 0 || loading}
              onPress={handlePanicExit}
            >
              <Text style={styles.btnPrimaryText}>Square Off All (Panic Exit)</Text>
            </Pressable>

            {(appState?.trades?.length ?? 0) === 0 ? (
              <View style={styles.emptyBox}>
                <Text style={styles.emptyText}>No {mode} trades logged today yet.</Text>
              </View>
            ) : (
              appState?.trades.map((tr) => (
                <View key={tr.trade_id} style={styles.tradeItem}>
                  <View style={styles.signalHead}>
                    <Text style={styles.signalSymbol}>
                      {tr.symbol} ({tr.qty} qty)
                    </Text>
                    <Text
                      style={[
                        styles.tradePnl,
                        { color: tr.pnl >= 0 ? "#16A34A" : "#DC2626" },
                      ]}
                    >
                      {formatINR(tr.pnl, true)}
                    </Text>
                  </View>
                  <Text style={styles.signalDetail}>
                    Entry ₹{tr.entry_price.toFixed(2)} • LTP/Exit ₹{tr.ltp_or_exit.toFixed(2)} • SL ₹{tr.stop_loss.toFixed(2)} • Tgt ₹{tr.target.toFixed(2)}
                  </Text>
                  <Text style={styles.caption}>
                    {tr.status} {tr.exit_reason ? `(${tr.exit_reason})` : ""} • {tr.timestamp}
                  </Text>
                </View>
              ))
            )}
          </View>
        )}

        {/* TAB 3: OPTIONAL F&O TRADING */}
        {tab === "fno" && (
          <View style={styles.sectionCard}>
            <Text style={styles.sectionTitle}>F&amp;O Trading (Optional)</Text>
            <Text style={styles.caption}>
              Option Buying (CE/PE) &amp; 1-Lot Futures only • Daily Max Loss -₹1,500 • Status:{" "}
              {fnoState?.status || "IDLE"}
            </Text>

            <View style={styles.buttonRow}>
              {(["NIFTY", "BANKNIFTY", "FINNIFTY"] as const).map((idx) => (
                <Pressable
                  key={idx}
                  style={[styles.btnSmall, fnoUnderlying === idx && styles.btnPrimary]}
                  onPress={() => setFnoUnderlying(idx)}
                >
                  <Text style={fnoUnderlying === idx ? styles.btnPrimaryText : styles.btnOutlineText}>
                    {idx}
                  </Text>
                </Pressable>
              ))}
              {(["OPTIDX", "FUTIDX"] as const).map((inst) => (
                <Pressable
                  key={inst}
                  style={[styles.btnSmall, fnoInstrument === inst && styles.btnPrimary]}
                  onPress={() => setFnoInstrument(inst)}
                >
                  <Text style={fnoInstrument === inst ? styles.btnPrimaryText : styles.btnOutlineText}>
                    {inst === "OPTIDX" ? "Options" : "Futures"}
                  </Text>
                </Pressable>
              ))}
            </View>

            <Pressable style={[styles.btn, styles.btnPrimary]} onPress={handleAnalyzeFno}>
              <Text style={styles.btnPrimaryText}>Analyze Spot &amp; Load Contracts</Text>
            </Pressable>

            {fnoTrend && (
              <View style={[styles.banner, styles.bannerInfo]}>
                <Text style={styles.bannerTextInfo}>
                  {fnoTrend.index_name}: {fnoTrend.price} | {fnoTrend.regime} | RSI {fnoTrend.rsi_14} →{" "}
                  {fnoTrend.recommended_fno_action}
                </Text>
              </View>
            )}

            {fnoContracts.length > 0 && (
              <>
                <View style={styles.buttonRow}>
                  {fnoContracts.map((c) => {
                    const sel = c.tradingsymbol === fnoSelectedTsym;
                    return (
                      <Pressable
                        key={c.tradingsymbol}
                        style={[styles.btnSmall, sel && styles.btnPrimary]}
                        onPress={() => {
                          setFnoSelectedTsym(c.tradingsymbol);
                          if (c.ltp && c.ltp > 0) setFnoEntryPx(String(c.ltp));
                        }}
                      >
                        <Text style={sel ? styles.btnPrimaryText : styles.btnOutlineText}>
                          {c.tradingsymbol}
                        </Text>
                      </Pressable>
                    );
                  })}
                </View>

                <View style={styles.row3}>
                  <View style={{ flex: 1 }}>
                    <Text style={styles.inputLabel}>Entry ₹</Text>
                    <TextInput
                      style={styles.input}
                      value={fnoEntryPx}
                      onChangeText={setFnoEntryPx}
                      keyboardType="numeric"
                    />
                  </View>
                  <View style={{ flex: 1 }}>
                    <Text style={styles.inputLabel}>SL (Pts)</Text>
                    <TextInput
                      style={styles.input}
                      value={fnoSlPoints}
                      onChangeText={setFnoSlPoints}
                      keyboardType="numeric"
                    />
                  </View>
                  <View style={{ flex: 1 }}>
                    <Text style={styles.inputLabel}>Lots (≤2)</Text>
                    <TextInput
                      style={styles.input}
                      value={fnoLots}
                      onChangeText={setFnoLots}
                      keyboardType="numeric"
                    />
                  </View>
                </View>

                <View style={styles.switchRow}>
                  <Text style={styles.switchLabel}>Confirm 1-lot F&amp;O risk &amp; strict SL</Text>
                  <Switch value={fnoAck} onValueChange={setFnoAck} />
                </View>

                <View style={styles.buttonRow}>
                  <Pressable
                    style={[styles.btn, styles.btnExecute, (!fnoAck || loading) && styles.btnDisabled]}
                    disabled={!fnoAck || loading}
                    onPress={handleExecuteFno}
                  >
                    <Text style={styles.btnPrimaryText}>Execute F&amp;O BUY</Text>
                  </Pressable>
                  <Pressable
                    style={[
                      styles.btn,
                      styles.btnPanic,
                      ((fnoState?.summary?.open_positions_count ?? 0) === 0 || loading) && styles.btnDisabled,
                    ]}
                    disabled={(fnoState?.summary?.open_positions_count ?? 0) === 0 || loading}
                    onPress={handlePanicFno}
                  >
                    <Text style={styles.btnPrimaryText}>Square Off F&amp;O</Text>
                  </Pressable>
                </View>
              </>
            )}

            {fnoState?.trades?.map((tr) => (
              <View key={tr.trade_id} style={styles.tradeItem}>
                <View style={styles.signalHead}>
                  <Text style={styles.signalSymbol}>
                    {tr.tradingsymbol} ({tr.qty})
                  </Text>
                  <Text style={[styles.tradePnl, { color: tr.pnl >= 0 ? "#16A34A" : "#DC2626" }]}>
                    {formatINR(tr.pnl, true)}
                  </Text>
                </View>
                <Text style={styles.caption}>
                  Entry ₹{tr.entry_price} • SL ₹{tr.stop_loss} • Tgt ₹{tr.target} • {tr.status}
                </Text>
              </View>
            ))}
          </View>
        )}

        {/* TAB 4: HTTPS SERVER & ANGEL ONE CREDENTIALS */}
        {tab === "settings" && (
          <View style={styles.sectionCard}>
            <Text style={styles.sectionTitle}>4G/5G HTTPS Backend Connection</Text>
            <Text style={styles.caption}>
              Enter your cloud or tunnel HTTPS URL (e.g. https://your-api.onrender.com) and MOBILE_API_TOKEN from .env. Stored in iOS Keychain / Android Keystore.
            </Text>

            <Text style={styles.inputLabel}>Backend HTTPS URL</Text>
            <TextInput
              style={styles.input}
              value={config.baseUrl}
              onChangeText={(v) => setConfig((c) => ({ ...c, baseUrl: v }))}
              placeholder="https://your-server.trycloudflare.com"
              autoCapitalize="none"
              autoCorrect={false}
            />

            <Text style={styles.inputLabel}>Mobile API Token (Bearer)</Text>
            <TextInput
              style={styles.input}
              value={config.apiToken}
              onChangeText={(v) => setConfig((c) => ({ ...c, apiToken: v }))}
              placeholder="Paste MOBILE_API_TOKEN from .env"
              autoCapitalize="none"
              secureTextEntry
            />

            <View style={styles.row3}>
              <View style={{ flex: 1 }}>
                <Text style={styles.inputLabel}>Capital (₹)</Text>
                <TextInput
                  style={styles.input}
                  value={config.capital}
                  onChangeText={(v) => setConfig((c) => ({ ...c, capital: v }))}
                  keyboardType="numeric"
                />
              </View>
              <View style={{ flex: 1 }}>
                <Text style={styles.inputLabel}>Max SL (≤400)</Text>
                <TextInput
                  style={styles.input}
                  value={config.maxSl}
                  onChangeText={(v) => setConfig((c) => ({ ...c, maxSl: v }))}
                  keyboardType="numeric"
                />
              </View>
              <View style={{ flex: 1 }}>
                <Text style={styles.inputLabel}>Trades (≤2)</Text>
                <TextInput
                  style={styles.input}
                  value={config.maxTrades}
                  onChangeText={(v) => setConfig((c) => ({ ...c, maxTrades: v }))}
                  keyboardType="numeric"
                />
              </View>
            </View>

            <Text style={styles.inputLabel}>Active Trading Window (IST)</Text>
            <View style={styles.buttonRow}>
              {(
                [
                  ["morning", "09:20–11:30"],
                  ["split", "Morning + 13:45–14:55"],
                  ["full", "09:20–15:00 Full"],
                ] as const
              ).map(([key, label]) => {
                const selected = config.windowPreset === key;
                return (
                  <Pressable
                    key={key}
                    style={[styles.btnSmall, selected && styles.btnPrimary]}
                    onPress={() => setConfig((c) => ({ ...c, windowPreset: key }))}
                  >
                    <Text style={selected ? styles.btnPrimaryText : styles.btnOutlineText}>{label}</Text>
                  </Pressable>
                );
              })}
            </View>

            <Text style={styles.inputLabel}>Stock Screener Universe</Text>
            <View style={styles.buttonRow}>
              {(
                [
                  ["top15", "Top 15"],
                  ["nifty50", "Full Nifty 50"],
                  ["custom", "Custom"],
                ] as const
              ).map(([key, label]) => {
                const selected = config.universePreset === key;
                return (
                  <Pressable
                    key={key}
                    style={[styles.btnSmall, selected && styles.btnPrimary]}
                    onPress={() => setConfig((c) => ({ ...c, universePreset: key }))}
                  >
                    <Text style={selected ? styles.btnPrimaryText : styles.btnOutlineText}>{label}</Text>
                  </Pressable>
                );
              })}
            </View>

            {config.universePreset === "custom" && (
              <>
                <Text style={styles.inputLabel}>Custom NSE Symbols (comma-separated)</Text>
                <TextInput
                  style={styles.input}
                  value={config.customSymbols}
                  onChangeText={(v) => setConfig((c) => ({ ...c, customSymbols: v }))}
                  placeholder="TRENT, BEL, HAL, TITAN"
                  autoCapitalize="characters"
                />
              </>
            )}

            <View style={styles.switchRow}>
              <Text style={styles.switchLabel}>Trailing SL + Break-Even (+1R/+1.5R)</Text>
              <Switch
                value={config.trailingSlEnabled}
                onValueChange={(v) => setConfig((c) => ({ ...c, trailingSlEnabled: v }))}
              />
            </View>
            <View style={styles.switchRow}>
              <Text style={styles.switchLabel}>NIFTY 50 Index Trend Filter</Text>
              <Switch
                value={config.niftyTrendFilter}
                onValueChange={(v) => setConfig((c) => ({ ...c, niftyTrendFilter: v }))}
              />
            </View>

            <Pressable style={[styles.btn, styles.btnPrimary]} onPress={handleSaveServer}>
              <Text style={styles.btnPrimaryText}>Save & Connect Server</Text>
            </Pressable>

            <View style={styles.divider} />

            <Text style={styles.sectionTitle}>Angel One SmartAPI Credentials</Text>
            <Text style={styles.caption}>
              Saved directly to the backend .env file over HTTPS for Live Trading.
            </Text>

            <Text style={styles.inputLabel}>API Key</Text>
            <TextInput
              style={styles.input}
              value={apiKey}
              onChangeText={setApiKey}
              secureTextEntry
              autoCapitalize="none"
            />
            <Text style={styles.inputLabel}>Client ID</Text>
            <TextInput
              style={styles.input}
              value={clientId}
              onChangeText={setClientId}
              autoCapitalize="characters"
            />
            <Text style={styles.inputLabel}>Password / PIN</Text>
            <TextInput
              style={styles.input}
              value={password}
              onChangeText={setPassword}
              secureTextEntry
            />
            <Text style={styles.inputLabel}>TOTP Secret</Text>
            <TextInput
              style={styles.input}
              value={totpSecret}
              onChangeText={setTotpSecret}
              secureTextEntry
              autoCapitalize="none"
            />

            <View style={styles.buttonRow}>
              <Pressable style={[styles.btn, styles.btnPrimary]} onPress={handleConnectBroker}>
                <Text style={styles.btnPrimaryText}>Save .env & Connect SmartAPI</Text>
              </Pressable>
              {appState?.broker_connected && (
                <Pressable style={[styles.btn, styles.btnOutline]} onPress={handleDisconnectBroker}>
                  <Text style={styles.btnOutlineText}>Disconnect</Text>
                </Pressable>
              )}
            </View>
          </View>
        )}
      </ScrollView>

      {/* Bottom Tab Bar (Thumb-friendly on iPhone 14 Plus & OnePlus Pad Go) */}
      <View style={styles.tabBar}>
        {(
          [
            ["signals", "Signals"],
            ["trades", `Trades (${openCount})`],
            ["fno", "F&O"],
            ["settings", "Settings"],
          ] as [TabKey, string][]
        ).map(([key, label]) => {
          const active = tab === key;
          return (
            <Pressable
              key={key}
              style={[styles.tabItem, active && styles.tabItemActive]}
              onPress={() => setTab(key)}
            >
              <Text style={[styles.tabText, active && styles.tabTextActive]}>{label}</Text>
            </Pressable>
          );
        })}
      </View>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  safe: { flex: 1, backgroundColor: "#F8FAFC" },
  header: {
    flexDirection: "row",
    alignItems: "center",
    paddingHorizontal: 16,
    paddingVertical: 12,
    backgroundColor: "#FFFFFF",
    borderBottomWidth: 1,
    borderBottomColor: "#E2E8F0",
    gap: 10,
  },
  title: { fontSize: 18, fontWeight: "700", color: "#0F172A" },
  subtitle: { fontSize: 12, color: "#64748B", marginTop: 2 },
  modeBox: {
    flexDirection: "row",
    alignItems: "center",
    paddingHorizontal: 10,
    paddingVertical: 4,
    borderRadius: 12,
    borderWidth: 1,
    gap: 6,
  },
  modePaper: { backgroundColor: "#F0FDF4", borderColor: "#86EFAC" },
  modeLive: { backgroundColor: "#FEF2F2", borderColor: "#FCA5A5" },
  modeLabel: { fontSize: 11, fontWeight: "700" },
  scroll: { flex: 1 },
  scrollContent: { padding: 14, paddingBottom: 28, gap: 12 },
  scrollContentTablet: { maxWidth: 1000, alignSelf: "center", width: "100%" },
  banner: { padding: 12, borderRadius: 10, borderWidth: 1 },
  bannerSuccess: { backgroundColor: "#F0FDF4", borderColor: "#86EFAC" },
  bannerError: { backgroundColor: "#FEF2F2", borderColor: "#FCA5A5" },
  bannerWarn: { backgroundColor: "#FFFBEB", borderColor: "#FDE68A" },
  bannerInfo: { backgroundColor: "#EFF6FF", borderColor: "#BFDBFE" },
  bannerTextSuccess: { color: "#166534", fontSize: 13, fontWeight: "600" },
  bannerTextError: { color: "#991B1B", fontSize: 13, fontWeight: "600" },
  bannerTextWarn: { color: "#92400E", fontSize: 13, fontWeight: "600" },
  bannerTextInfo: { color: "#1E40AF", fontSize: 13, fontWeight: "600" },
  metricGrid: { flexDirection: "row", flexWrap: "wrap", gap: 10 },
  metricCard: {
    width: "48.5%",
    backgroundColor: "#FFFFFF",
    borderRadius: 12,
    padding: 12,
    borderWidth: 1,
    borderColor: "#E2E8F0",
  },
  metricCardTablet: { width: "23.8%" },
  metricLabel: { fontSize: 12, color: "#64748B", fontWeight: "600" },
  metricValue: { fontSize: 19, fontWeight: "700", color: "#0F172A", marginTop: 4 },
  metricSub: { fontSize: 11, color: "#64748B", marginTop: 3 },
  loadingRow: { flexDirection: "row", alignItems: "center", gap: 8, paddingHorizontal: 4 },
  loadingText: { fontSize: 12, color: "#475569" },
  sectionCard: {
    backgroundColor: "#FFFFFF",
    borderRadius: 14,
    padding: 14,
    borderWidth: 1,
    borderColor: "#E2E8F0",
    gap: 10,
  },
  sectionTitle: { fontSize: 16, fontWeight: "700", color: "#0F172A" },
  caption: { fontSize: 12, color: "#64748B" },
  buttonRow: { flexDirection: "row", flexWrap: "wrap", gap: 10 },
  btn: {
    paddingVertical: 12,
    paddingHorizontal: 16,
    borderRadius: 10,
    alignItems: "center",
    justifyContent: "center",
    flexGrow: 1,
  },
  btnSmall: {
    paddingVertical: 7,
    paddingHorizontal: 12,
    borderRadius: 8,
    borderWidth: 1,
    borderColor: "#CBD5E1",
  },
  btnPrimary: { backgroundColor: "#0F172A" },
  btnExecute: { backgroundColor: "#0D9488", marginTop: 8 },
  btnPanic: { backgroundColor: "#DC2626" },
  btnOutline: { backgroundColor: "#FFFFFF", borderWidth: 1, borderColor: "#CBD5E1" },
  btnDisabled: { opacity: 0.45 },
  btnPrimaryText: { color: "#FFFFFF", fontWeight: "700", fontSize: 14 },
  btnOutlineText: { color: "#0F172A", fontWeight: "600", fontSize: 13 },
  switchRow: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    paddingVertical: 4,
  },
  switchLabel: { fontSize: 14, fontWeight: "600", color: "#1E293B" },
  emptyBox: {
    backgroundColor: "#F8FAFC",
    padding: 14,
    borderRadius: 10,
    borderWidth: 1,
    borderColor: "#E2E8F0",
  },
  emptyText: { fontSize: 13, color: "#475569", lineHeight: 19 },
  signalItem: {
    backgroundColor: "#F8FAFC",
    borderRadius: 12,
    padding: 12,
    borderWidth: 1,
    borderColor: "#CBD5E1",
    gap: 4,
  },
  signalHead: { flexDirection: "row", justifyContent: "space-between", alignItems: "center" },
  signalSymbol: { fontSize: 16, fontWeight: "700", color: "#0F172A" },
  signalBadge: {
    fontSize: 11,
    fontWeight: "700",
    color: "#0F766E",
    backgroundColor: "#CCFBF1",
    paddingHorizontal: 8,
    paddingVertical: 3,
    borderRadius: 6,
  },
  signalMetrics: { flexDirection: "row", gap: 14, marginTop: 4 },
  signalDetail: { fontSize: 13, fontWeight: "600", color: "#334155" },
  diagToggle: { paddingVertical: 8 },
  diagToggleText: { fontSize: 13, fontWeight: "600", color: "#2563EB" },
  diagRow: {
    flexDirection: "row",
    justifyContent: "space-between",
    paddingVertical: 6,
    borderTopWidth: 1,
    borderTopColor: "#F1F5F9",
  },
  diagSym: { fontSize: 13, fontWeight: "700", color: "#0F172A" },
  diagMeta: { fontSize: 12, color: "#475569" },
  tradeHeader: { flexDirection: "row", justifyContent: "space-between", alignItems: "center" },
  tradeItem: {
    backgroundColor: "#F8FAFC",
    borderRadius: 10,
    padding: 12,
    borderWidth: 1,
    borderColor: "#E2E8F0",
    gap: 4,
  },
  tradePnl: { fontSize: 15, fontWeight: "700" },
  inputLabel: { fontSize: 12, fontWeight: "600", color: "#334155", marginTop: 4 },
  input: {
    backgroundColor: "#F8FAFC",
    borderWidth: 1,
    borderColor: "#CBD5E1",
    borderRadius: 10,
    paddingHorizontal: 12,
    paddingVertical: 10,
    fontSize: 14,
    color: "#0F172A",
  },
  row3: { flexDirection: "row", gap: 8 },
  divider: { height: 1, backgroundColor: "#E2E8F0", marginVertical: 8 },
  tabBar: {
    flexDirection: "row",
    backgroundColor: "#FFFFFF",
    borderTopWidth: 1,
    borderTopColor: "#E2E8F0",
    paddingBottom: 14,
    paddingTop: 8,
    paddingHorizontal: 12,
    gap: 8,
  },
  tabItem: {
    flex: 1,
    paddingVertical: 10,
    borderRadius: 10,
    alignItems: "center",
    backgroundColor: "#F1F5F9",
  },
  tabItemActive: { backgroundColor: "#0F172A" },
  tabText: { fontSize: 13, fontWeight: "700", color: "#475569" },
  tabTextActive: { color: "#FFFFFF" },
});
