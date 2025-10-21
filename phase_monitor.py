#!/usr/bin/env python3
"""
3-Fase Monitor voor 3x25A Aansluiting Check
============================================

Doel: Monitoren of downgrade van 3x40A naar 3x25A mogelijk is
Limiet per fase: 25A × 230V = 5750W (veilig: 5100W met marge)

Features:
- Real-time fase monitoring
- Automatische overschrijding logging
- Statistieken per dag/week/maand
- Dashboard visualisatie
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Any

logger = logging.getLogger("phase-monitor")

# Configuratie
PHASE_LIMIT_W = 5100  # Veilige limiet per fase (25A × 230V × 0.88)
LOG_FILE = Path(__file__).parent / "phase_monitor.json"
VIOLATIONS_LOG_FILE = Path(__file__).parent / "phase_violations.json"  # Permanent log
PEAK_VALUES_FILE = Path(__file__).parent / "phase_peaks.json"  # Permanent peak tracking
MONITOR_INTERVAL_S = 5  # Check elke 5 seconden
MAX_LOG_ENTRIES = 10000  # Bewaar max 10k entries (ca. 2 dagen)
MAX_VIOLATIONS = 1000  # Bewaar max 1000 violations (permanent)


class PhaseMonitor:
    """Monitort 3-fase belasting en logt overschrijdingen"""
    
    def __init__(self, myenergi_client, myenergi_lock, p1_reader=None):
        self.myenergi = myenergi_client
        self.myenergi_lock = myenergi_lock
        self.p1_reader = p1_reader  # Optional P1 meter reader
        self.running = False
        self.task = None
        
        # Statistics
        self.stats = {
            "total_checks": 0,
            "violations_count": 0,
            "last_violation": None,
            "max_l1": 0,
            "max_l2": 0,
            "max_l3": 0,
            "started_at": None
        }
        
        # Load existing logs
        self.log = self._load_log()
        self.violations_log = self._load_violations_log()
        self.peak_values = self._load_peak_values()
    
    def _load_log(self) -> List[Dict]:
        """Laad bestaande log entries"""
        try:
            if LOG_FILE.exists():
                with open(LOG_FILE, 'r') as f:
                    return json.load(f)
        except Exception as e:
            logger.warning(f"Could not load phase log: {e}")
        return []
    
    def _load_violations_log(self) -> List[Dict]:
        """Laad permanent violations log"""
        try:
            if VIOLATIONS_LOG_FILE.exists():
                with open(VIOLATIONS_LOG_FILE, 'r') as f:
                    return json.load(f)
        except Exception as e:
            logger.warning(f"Could not load violations log: {e}")
        return []
    
    def _load_peak_values(self) -> Dict:
        """Laad permanent peak values (NOOIT gereset)"""
        try:
            if PEAK_VALUES_FILE.exists():
                with open(PEAK_VALUES_FILE, 'r') as f:
                    data = json.load(f)
                    # Ensure peak_history exists (backward compatibility)
                    if "peak_history" not in data:
                        data["peak_history"] = {
                            "l1": [],
                            "l2": [],
                            "l3": []
                        }
                    return data
        except Exception as e:
            logger.warning(f"Could not load peak values: {e}")
        
        # Default structure
        return {
            "max_l1": 0,
            "max_l1_timestamp": None,
            "max_l2": 0,
            "max_l2_timestamp": None,
            "max_l3": 0,
            "max_l3_timestamp": None,
            "last_updated": None,
            "peak_history": {
                "l1": [],  # [{value: 100, timestamp: "..."}, {value: 200, ...}]
                "l2": [],
                "l3": []
            }
        }
    
    def _save_peak_values(self):
        """Sla peak values op (NOOIT verwijderd)"""
        try:
            with open(PEAK_VALUES_FILE, 'w') as f:
                json.dump(self.peak_values, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save peak values: {e}")
    
    def _save_log(self):
        """Sla log op naar JSON file"""
        try:
            # Trim oude entries
            if len(self.log) > MAX_LOG_ENTRIES:
                self.log = self.log[-MAX_LOG_ENTRIES:]
            
            with open(LOG_FILE, 'w') as f:
                json.dump(self.log, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save phase log: {e}")
    
    def _save_violation(self, entry: Dict):
        """Sla violation op in permanent log (NOOIT verwijderd)"""
        try:
            self.violations_log.append(entry)
            
            # Trim alleen als echt nodig (keep laatste 1000)
            if len(self.violations_log) > MAX_VIOLATIONS:
                self.violations_log = self.violations_log[-MAX_VIOLATIONS:]
            
            self._save_violations_log()
            
            logger.info(f"💾 Violation opgeslagen in permanent log")
        except Exception as e:
            logger.error(f"Failed to save violation: {e}")
    
    def _save_violations_log(self):
        """Sla violations log op naar bestand (voor updates van bestaande violations)"""
        try:
            with open(VIOLATIONS_LOG_FILE, 'w') as f:
                json.dump(self.violations_log, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save violations log: {e}")
    
    async def _get_zappi_power(self) -> int:
        """Haal Zappi vermogen op (voor load balancing check)"""
        try:
            async with self.myenergi_lock:
                data = await self.myenergi.status_all()
            
            raw = data.get("raw", [])
            for section in raw if isinstance(raw, list) else []:
                if isinstance(section, dict) and "zappi" in section:
                    zappi_list = section.get("zappi") or []
                    for zappi in zappi_list:
                        # ectp1 = Zappi charging power
                        ectp1 = zappi.get("ectp1", 0)
                        if ectp1:
                            return abs(int(ectp1))
            return 0
        except Exception:
            return 0
    
    async def _read_phases(self) -> Optional[Dict]:
        """Lees fase data van P1 meter / Zappi / Harvi (in volgorde van voorkeur)"""
        try:
            # Priority 1: P1 meter (most accurate)
            if self.p1_reader:
                try:
                    p1_data = await self.p1_reader.get_phase_data()
                    if p1_data and p1_data.get("l1_w") is not None:
                        logger.debug(f"Using P1 data: L1={p1_data['l1_w']}W L2={p1_data['l2_w']}W L3={p1_data['l3_w']}W")
                        return {
                            "l1_w": p1_data["l1_w"],
                            "l2_w": p1_data["l2_w"],
                            "l3_w": p1_data["l3_w"],
                            "source": "P1 meter"
                        }
                except Exception as e:
                    logger.debug(f"P1 read failed: {e}")
            
            # Priority 2: Zappi Grid CT clamps (ectp4, ectp5, ectp6)
            async with self.myenergi_lock:
                data = await self.myenergi.status_all()
            
            raw = data.get("raw", [])
            
            # Check Zappi for grid CT clamps (ectp4/5/6 = Grid per fase)
            for section in raw if isinstance(raw, list) else []:
                if isinstance(section, dict) and "zappi" in section:
                    zappi_list = section.get("zappi") or []
                    for zappi in zappi_list:
                        # ACTUAL MAPPING (verified with P1 meter):
                        # ectp4 = Fase B, ectp5 = Fase A, ectp6 = Fase C
                        # Dashboard expects: L1, L2, L3
                        # Correct mapping: ectp4(B)→L1, ectp5(A)→L2, ectp6(C)→L3
                        ectp4 = zappi.get("ectp4")  # Fase B
                        ectp5 = zappi.get("ectp5")  # Fase A
                        ectp6 = zappi.get("ectp6")  # Fase C
                        
                        # If Zappi has grid CT data, use it!
                        if ectp4 is not None or ectp5 is not None or ectp6 is not None:
                            l1 = int(ectp4) if ectp4 is not None else 0  # Fase B
                            l2 = int(ectp5) if ectp5 is not None else 0  # Fase A
                            l3 = int(ectp6) if ectp6 is not None else 0  # Fase C
                            logger.debug(f"Using Zappi Grid CT: L1(FaseB)={l1}W L2(FaseA)={l2}W L3(FaseC)={l3}W")
                            return {
                                "l1_w": l1,
                                "l2_w": l2,
                                "l3_w": l3,
                                "source": "Zappi Grid CT"
                            }
            
            # Priority 3: Harvi CT clamps (fallback)
            for section in raw if isinstance(raw, list) else []:
                if isinstance(section, dict) and "harvi" in section:
                    harvi_list = section.get("harvi") or []
                    for harvi in harvi_list:
                        ectp1 = harvi.get("ectp1")
                        ectp2 = harvi.get("ectp2")
                        ectp3 = harvi.get("ectp3")
                        
                        if ectp1 is not None:
                            logger.debug(f"Using Harvi CT: L1={ectp1}W L2={ectp2}W L3={ectp3}W")
                            return {
                                "l1_w": int(ectp1) if ectp1 is not None else 0,
                                "l2_w": int(ectp2) if ectp2 is not None else 0,
                                "l3_w": int(ectp3) if ectp3 is not None else 0,
                                "source": "Harvi CT"
                            }
            
            return None
        except Exception as e:
            logger.error(f"Error reading phases: {e}")
            return None
    
    def _check_violations(self, phases: Dict) -> List[str]:
        """Check welke fases over de limiet gaan"""
        violations = []
        
        l1 = phases.get("l1_w")
        l2 = phases.get("l2_w")
        l3 = phases.get("l3_w")
        
        if l1 is not None and abs(l1) > PHASE_LIMIT_W:
            violations.append(f"L1: {l1}W (>{PHASE_LIMIT_W}W)")
        if l2 is not None and abs(l2) > PHASE_LIMIT_W:
            violations.append(f"L2: {l2}W (>{PHASE_LIMIT_W}W)")
        if l3 is not None and abs(l3) > PHASE_LIMIT_W:
            violations.append(f"L3: {l3}W (>{PHASE_LIMIT_W}W)")
        
        return violations
    
    def _log_entry(self, phases: Dict, violations: List[str], zappi_power_w: int = 0):
        """Log entry (alleen als er overschrijding is OF elke 5 minuten)"""
        now = datetime.now()
        
        # Check if Zappi is charging (load balancing active)
        zappi_charging = zappi_power_w > 500  # > 500W = aan het laden
        
        # Update peak values ALLEEN als:
        # - GEEN violations, OF
        # - Violations maar Zappi NIET aan het laden (echte violation)
        # Reden: Zappi violations zijn niet representatief voor huis belasting
        is_zappi_violation = len(violations) > 0 and zappi_charging
        
        if not is_zappi_violation:
            # Dit is een echte meting zonder Zappi storing
            self._update_peak_values(phases, now)
        else:
            # Violation maar door Zappi → skip peak update
            logger.debug(f"⏭️ Skip peak update (Zappi violation): L1={phases.get('l1_w')}W L2={phases.get('l2_w')}W L3={phases.get('l3_w')}W")
        
        # Always log violations
        should_log = len(violations) > 0
        
        # Also log every 5 minutes for baseline
        if not should_log and self.log:
            last_entry_time = datetime.fromisoformat(self.log[-1]["timestamp"])
            if (now - last_entry_time).total_seconds() >= 300:  # 5 min
                should_log = True
        elif not self.log:
            should_log = True  # First entry
        
        if should_log:
            entry = {
                "timestamp": now.isoformat(),
                "l1_w": phases.get("l1_w"),
                "l2_w": phases.get("l2_w"),
                "l3_w": phases.get("l3_w"),
                "zappi_w": zappi_power_w,
                "zappi_charging": zappi_charging,
                "violations": violations,
                "is_violation": len(violations) > 0,
                "is_real_violation": len(violations) > 0 and not zappi_charging  # ECHT probleem
            }
            self.log.append(entry)
            self._save_log()
            
            if violations:
                # Sla ook op in PERMANENT violations log
                self._save_violation(entry)
                
                if zappi_charging:
                    logger.info(f"ℹ️ Fase overschrijding MET Zappi laden ({zappi_power_w}W) - Load balancing actief: {', '.join(violations)}")
                else:
                    logger.warning(f"⚠️ ECHTE FASE OVERSCHRIJDING (zonder Zappi): {', '.join(violations)}")
    
    def _update_peak_values(self, phases: Dict, timestamp: datetime):
        """Update permanent peak values (alleen echte huishoudelijke pieken, GEEN Zappi violations)"""
        updated = False
        
        l1 = abs(phases.get("l1_w", 0))
        l2 = abs(phases.get("l2_w", 0))
        l3 = abs(phases.get("l3_w", 0))
        
        old_l1 = self.peak_values["max_l1"]
        old_l2 = self.peak_values["max_l2"]
        old_l3 = self.peak_values["max_l3"]
        
        if l1 > old_l1:
            self.peak_values["max_l1"] = l1
            self.peak_values["max_l1_timestamp"] = timestamp.isoformat()
            # Add to history
            self.peak_values["peak_history"]["l1"].append({
                "value": l1,
                "timestamp": timestamp.isoformat(),
                "previous": old_l1
            })
            logger.info(f"📈 NIEUWE ECHTE PEAK Fase B: {l1}W (was {old_l1}W)")
            updated = True
        
        if l2 > old_l2:
            self.peak_values["max_l2"] = l2
            self.peak_values["max_l2_timestamp"] = timestamp.isoformat()
            # Add to history
            self.peak_values["peak_history"]["l2"].append({
                "value": l2,
                "timestamp": timestamp.isoformat(),
                "previous": old_l2
            })
            logger.info(f"📈 NIEUWE ECHTE PEAK Fase A: {l2}W (was {old_l2}W)")
            updated = True
        
        if l3 > old_l3:
            self.peak_values["max_l3"] = l3
            self.peak_values["max_l3_timestamp"] = timestamp.isoformat()
            # Add to history
            self.peak_values["peak_history"]["l3"].append({
                "value": l3,
                "timestamp": timestamp.isoformat(),
                "previous": old_l3
            })
            logger.info(f"📈 NIEUWE ECHTE PEAK Fase C: {l3}W (was {old_l3}W)")
            updated = True
        
        if updated:
            self.peak_values["last_updated"] = timestamp.isoformat()
            self._save_peak_values()
    
    async def _monitor_loop(self):
        """Main monitoring loop"""
        logger.info(f"🔌 Fase monitor gestart - Limiet: {PHASE_LIMIT_W}W per fase")
        self.stats["started_at"] = datetime.now().isoformat()
        
        while self.running:
            try:
                # Read phase data
                phases = await self._read_phases()
                if phases and any(v is not None for v in phases.values()):
                    # Get Zappi power (for load balancing context)
                    zappi_power = await self._get_zappi_power()
                    
                    # Check for violations
                    violations = self._check_violations(phases)
                    
                    # Log if needed (stats are calculated from log)
                    self._log_entry(phases, violations, zappi_power)
                
                # Wait before next check
                await asyncio.sleep(MONITOR_INTERVAL_S)
                
            except Exception as e:
                logger.error(f"❌ Phase monitor error: {e}")
                await asyncio.sleep(10)
    
    async def start(self):
        """Start de fase monitor"""
        if self.running:
            logger.warning("Phase monitor already running")
            return
        
        self.running = True
        self.task = asyncio.create_task(self._monitor_loop())
        logger.info("✅ Phase monitor started")
    
    async def stop(self):
        """Stop de fase monitor"""
        if not self.running:
            return
        
        self.running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        
        self._save_log()
        logger.info("🛑 Phase monitor stopped")
    
    def get_stats(self) -> Dict:
        """Haal huidige statistieken op (persistent via log + peak file)"""
        # Count violations from PERMANENT violations log
        violations_count = len(self.violations_log)
        
        # Find last violation timestamp from permanent log
        last_violation = None
        if self.violations_log:
            last_violation = self.violations_log[-1].get("timestamp")
        
        return {
            "total_checks": len(self.log),  # From log
            "violations_count": violations_count,  # From PERMANENT violations log
            "last_violation": last_violation,  # From PERMANENT violations log
            "max_l1": self.peak_values["max_l1"],  # From PERMANENT peak file
            "max_l2": self.peak_values["max_l2"],  # From PERMANENT peak file
            "max_l3": self.peak_values["max_l3"],  # From PERMANENT peak file
            "max_l1_timestamp": self.peak_values["max_l1_timestamp"],
            "max_l2_timestamp": self.peak_values["max_l2_timestamp"],
            "max_l3_timestamp": self.peak_values["max_l3_timestamp"],
            "peak_history_count": {
                "l1": len(self.peak_values["peak_history"]["l1"]),
                "l2": len(self.peak_values["peak_history"]["l2"]),
                "l3": len(self.peak_values["peak_history"]["l3"])
            },
            "started_at": self.stats["started_at"],  # Current session
            "limit_per_phase_w": PHASE_LIMIT_W,
            "log_entries": len(self.log),
            "running": self.running
        }
    
    def get_peak_history(self, phase: str = None) -> Dict:
        """Haal peak history op voor alle fases of specifieke fase"""
        if phase:
            phase = phase.lower()
            if phase in ["l1", "l2", "l3"]:
                return {
                    "phase": phase,
                    "history": self.peak_values["peak_history"][phase]
                }
            return {"error": "Invalid phase, use l1, l2, or l3"}
        
        # Return all histories
        return {
            "l1": self.peak_values["peak_history"]["l1"],
            "l2": self.peak_values["peak_history"]["l2"],
            "l3": self.peak_values["peak_history"]["l3"]
        }
    
    def get_top_peaks(self, limit: int = 50, include_below_limit: bool = True) -> Dict:
        """Haal TOP N hoogste pieken op (ook die ONDER de limiet blijven)
        
        Args:
            limit: Aantal top pieken per fase (default 50)
            include_below_limit: Include metingen onder de limiet (default True)
        
        Returns:
            Dict met top pieken per fase, gesorteerd van hoog naar laag
        """
        # Verzamel ALLE metingen met hun waarden
        all_peaks = []
        
        # Gebruik BEIDE logs: violations_log EN normale log
        all_entries = self.log + self.violations_log
        
        # Dedupliceer op timestamp
        seen_timestamps = set()
        unique_entries = []
        for entry in all_entries:
            ts = entry.get("timestamp")
            if ts not in seen_timestamps:
                seen_timestamps.add(ts)
                unique_entries.append(entry)
        
        for entry in unique_entries:
            timestamp = entry.get("timestamp")
            l1 = abs(entry.get("l1_w", 0) or 0)
            l2 = abs(entry.get("l2_w", 0) or 0)
            l3 = abs(entry.get("l3_w", 0) or 0)
            zappi_w = entry.get("zappi_w", 0)
            zappi_charging = entry.get("zappi_charging", False)
            is_real = entry.get("is_real_violation", False)
            violations = entry.get("violations", [])
            
            # L1 peak
            if l1 > 0 and (include_below_limit or l1 > PHASE_LIMIT_W):
                all_peaks.append({
                    "phase": "L1 (Fase B)",
                    "value_w": l1,
                    "limit_w": PHASE_LIMIT_W,
                    "distance_to_limit_w": PHASE_LIMIT_W - l1,
                    "distance_percent": round(((PHASE_LIMIT_W - l1) / PHASE_LIMIT_W) * 100, 1),
                    "over_limit": l1 > PHASE_LIMIT_W,
                    "timestamp": timestamp,
                    "zappi_w": zappi_w,
                    "zappi_charging": zappi_charging,
                    "is_real_violation": is_real,
                    "all_phases": {"l1_w": entry.get("l1_w"), "l2_w": entry.get("l2_w"), "l3_w": entry.get("l3_w")},
                    "violations_text": violations
                })
            
            # L2 peak
            if l2 > 0 and (include_below_limit or l2 > PHASE_LIMIT_W):
                all_peaks.append({
                    "phase": "L2 (Fase A)",
                    "value_w": l2,
                    "limit_w": PHASE_LIMIT_W,
                    "distance_to_limit_w": PHASE_LIMIT_W - l2,
                    "distance_percent": round(((PHASE_LIMIT_W - l2) / PHASE_LIMIT_W) * 100, 1),
                    "over_limit": l2 > PHASE_LIMIT_W,
                    "timestamp": timestamp,
                    "zappi_w": zappi_w,
                    "zappi_charging": zappi_charging,
                    "is_real_violation": is_real,
                    "all_phases": {"l1_w": entry.get("l1_w"), "l2_w": entry.get("l2_w"), "l3_w": entry.get("l3_w")},
                    "violations_text": violations
                })
            
            # L3 peak
            if l3 > 0 and (include_below_limit or l3 > PHASE_LIMIT_W):
                all_peaks.append({
                    "phase": "L3 (Fase C)",
                    "value_w": l3,
                    "limit_w": PHASE_LIMIT_W,
                    "distance_to_limit_w": PHASE_LIMIT_W - l3,
                    "distance_percent": round(((PHASE_LIMIT_W - l3) / PHASE_LIMIT_W) * 100, 1),
                    "over_limit": l3 > PHASE_LIMIT_W,
                    "timestamp": timestamp,
                    "zappi_w": zappi_w,
                    "zappi_charging": zappi_charging,
                    "is_real_violation": is_real,
                    "all_phases": {"l1_w": entry.get("l1_w"), "l2_w": entry.get("l2_w"), "l3_w": entry.get("l3_w")},
                    "violations_text": violations
                })
        
        # Sorteer van hoog naar laag
        peaks_sorted = sorted(all_peaks, key=lambda x: x["value_w"], reverse=True)
        
        # Split per fase en neem top N
        l1_top = [v for v in peaks_sorted if "L1" in v["phase"]][:limit]
        l2_top = [v for v in peaks_sorted if "L2" in v["phase"]][:limit]
        l3_top = [v for v in peaks_sorted if "L3" in v["phase"]][:limit]
        
        # Overall top (alle fases)
        overall_top = peaks_sorted[:limit]
        
        # Near-miss: hoge waarden die ONDER de limiet blijven (80-100% van limiet)
        near_miss_threshold = PHASE_LIMIT_W * 0.80  # 80% van limiet
        near_misses = [p for p in peaks_sorted if not p["over_limit"] and p["value_w"] > near_miss_threshold][:limit]
        
        return {
            "overall_top": overall_top,
            "l1_top": l1_top,
            "l2_top": l2_top,
            "l3_top": l3_top,
            "near_misses": near_misses,
            "near_miss_threshold_w": int(near_miss_threshold),
            "total_peaks": len(all_peaks),
            "violations_count": len([p for p in all_peaks if p["over_limit"]]),
            "limit": limit,
            "generated_at": datetime.now().isoformat()
        }
    
    def get_top_violations(self, limit: int = 20) -> Dict:
        """Haal TOP N ergste overschrijdingen op met alle details
        
        Args:
            limit: Aantal top violations per fase (default 20)
        
        Returns:
            Dict met top violations per fase, gesorteerd van hoog naar laag
        """
        # Verzamel ALLE violations met hun max waarde per fase
        violations_with_max = []
        
        for entry in self.violations_log:
            timestamp = entry.get("timestamp")
            l1 = abs(entry.get("l1_w", 0) or 0)
            l2 = abs(entry.get("l2_w", 0) or 0)
            l3 = abs(entry.get("l3_w", 0) or 0)
            zappi_w = entry.get("zappi_w", 0)
            zappi_charging = entry.get("zappi_charging", False)
            is_real = entry.get("is_real_violation", False)
            violations_text = entry.get("violations", [])
            
            # Voeg entry toe per fase als die fase violation had
            if l1 > PHASE_LIMIT_W:
                violations_with_max.append({
                    "phase": "L1 (Fase B)",
                    "value_w": l1,
                    "limit_w": PHASE_LIMIT_W,
                    "overshoot_w": l1 - PHASE_LIMIT_W,
                    "overshoot_percent": round(((l1 - PHASE_LIMIT_W) / PHASE_LIMIT_W) * 100, 1),
                    "timestamp": timestamp,
                    "zappi_w": zappi_w,
                    "zappi_charging": zappi_charging,
                    "is_real_violation": is_real,
                    "all_phases": {"l1_w": entry.get("l1_w"), "l2_w": entry.get("l2_w"), "l3_w": entry.get("l3_w")},
                    "violations_text": violations_text
                })
            
            if l2 > PHASE_LIMIT_W:
                violations_with_max.append({
                    "phase": "L2 (Fase A)",
                    "value_w": l2,
                    "limit_w": PHASE_LIMIT_W,
                    "overshoot_w": l2 - PHASE_LIMIT_W,
                    "overshoot_percent": round(((l2 - PHASE_LIMIT_W) / PHASE_LIMIT_W) * 100, 1),
                    "timestamp": timestamp,
                    "zappi_w": zappi_w,
                    "zappi_charging": zappi_charging,
                    "is_real_violation": is_real,
                    "all_phases": {"l1_w": entry.get("l1_w"), "l2_w": entry.get("l2_w"), "l3_w": entry.get("l3_w")},
                    "violations_text": violations_text
                })
            
            if l3 > PHASE_LIMIT_W:
                violations_with_max.append({
                    "phase": "L3 (Fase C)",
                    "value_w": l3,
                    "limit_w": PHASE_LIMIT_W,
                    "overshoot_w": l3 - PHASE_LIMIT_W,
                    "overshoot_percent": round(((l3 - PHASE_LIMIT_W) / PHASE_LIMIT_W) * 100, 1),
                    "timestamp": timestamp,
                    "zappi_w": zappi_w,
                    "zappi_charging": zappi_charging,
                    "is_real_violation": is_real,
                    "all_phases": {"l1_w": entry.get("l1_w"), "l2_w": entry.get("l2_w"), "l3_w": entry.get("l3_w")},
                    "violations_text": violations_text
                })
        
        # Sorteer van hoog naar laag
        violations_sorted = sorted(violations_with_max, key=lambda x: x["value_w"], reverse=True)
        
        # Split per fase en neem top N
        l1_top = [v for v in violations_sorted if "L1" in v["phase"]][:limit]
        l2_top = [v for v in violations_sorted if "L2" in v["phase"]][:limit]
        l3_top = [v for v in violations_sorted if "L3" in v["phase"]][:limit]
        
        # Overall top (alle fases)
        overall_top = violations_sorted[:limit]
        
        return {
            "overall_top": overall_top,
            "l1_top": l1_top,
            "l2_top": l2_top,
            "l3_top": l3_top,
            "total_violations": len(violations_with_max),
            "limit": limit,
            "generated_at": datetime.now().isoformat()
        }
    
    def get_violations(self, hours: int = 24) -> List[Dict]:
        """Haal overschrijdingen op van laatste X uur uit PERMANENT log"""
        cutoff = datetime.now() - timedelta(hours=hours)
        
        # Gebruik violations_log (permanent) ipv normale log
        violations = [
            entry for entry in self.violations_log
            if datetime.fromisoformat(entry["timestamp"]) > cutoff
        ]
        
        return violations
    
    def get_recent_data(self, count: int = 100) -> List[Dict]:
        """Haal laatste N entries op"""
        return self.log[-count:] if len(self.log) > count else self.log
    
    def analyze_peak_patterns(self) -> Dict:
        """Analyseer patronen in piekbelasting: tijdstippen, weekdagen, situaties
        
        BELANGRIJK: Gebruikt ALLEEN metingen ZONDER Zappi laden, omdat:
        - Zappi heeft load balancing en zou zich aanpassen aan 3x25A
        - We willen het echte huishoudelijke verbruik zien
        - Zappi metingen vertekenen het beeld
        """
        
        # Verzamel alle data
        all_entries = self.log + self.violations_log
        
        # Dedupliceer
        seen_timestamps = set()
        unique_entries = []
        for entry in all_entries:
            ts = entry.get("timestamp")
            if ts not in seen_timestamps:
                seen_timestamps.add(ts)
                unique_entries.append(entry)
        
        # FILTER: Alleen metingen ZONDER Zappi laden (>500W)
        # Dit is het echte huishoudelijke verbruik zonder load balancing verstoring
        filtered_entries = [
            e for e in unique_entries 
            if not e.get("zappi_charging", False)
        ]
        
        if not filtered_entries:
            return {"error": "Geen data zonder Zappi beschikbaar"}
        
        total_entries = len(unique_entries)
        filtered_count = len(filtered_entries)
        excluded_count = total_entries - filtered_count
        
        # Initialize pattern containers
        hour_stats = {h: {"count": 0, "max_l1": 0, "max_l2": 0, "max_l3": 0, "avg_l1": [], "avg_l2": [], "avg_l3": []} for h in range(24)}
        weekday_stats = {d: {"count": 0, "max_l1": 0, "max_l2": 0, "max_l3": 0, "avg_l1": [], "avg_l2": [], "avg_l3": []} for d in range(7)}
        period_stats = {
            "night": {"count": 0, "max_l1": 0, "max_l2": 0, "max_l3": 0, "peaks": []},      # 00-06
            "morning": {"count": 0, "max_l1": 0, "max_l2": 0, "max_l3": 0, "peaks": []},    # 06-12
            "afternoon": {"count": 0, "max_l1": 0, "max_l2": 0, "max_l3": 0, "peaks": []},  # 12-18
            "evening": {"count": 0, "max_l1": 0, "max_l2": 0, "max_l3": 0, "peaks": []}     # 18-24
        }
        
        # Analyze each entry (ZONDER Zappi)
        for entry in filtered_entries:
            try:
                dt = datetime.fromisoformat(entry.get("timestamp"))
                hour = dt.hour
                weekday = dt.weekday()  # 0=Monday, 6=Sunday
                
                l1 = abs(entry.get("l1_w", 0) or 0)
                l2 = abs(entry.get("l2_w", 0) or 0)
                l3 = abs(entry.get("l3_w", 0) or 0)
                
                max_phase = max(l1, l2, l3)
                
                # Hour statistics
                hour_stats[hour]["count"] += 1
                hour_stats[hour]["max_l1"] = max(hour_stats[hour]["max_l1"], l1)
                hour_stats[hour]["max_l2"] = max(hour_stats[hour]["max_l2"], l2)
                hour_stats[hour]["max_l3"] = max(hour_stats[hour]["max_l3"], l3)
                hour_stats[hour]["avg_l1"].append(l1)
                hour_stats[hour]["avg_l2"].append(l2)
                hour_stats[hour]["avg_l3"].append(l3)
                
                # Weekday statistics
                weekday_stats[weekday]["count"] += 1
                weekday_stats[weekday]["max_l1"] = max(weekday_stats[weekday]["max_l1"], l1)
                weekday_stats[weekday]["max_l2"] = max(weekday_stats[weekday]["max_l2"], l2)
                weekday_stats[weekday]["max_l3"] = max(weekday_stats[weekday]["max_l3"], l3)
                weekday_stats[weekday]["avg_l1"].append(l1)
                weekday_stats[weekday]["avg_l2"].append(l2)
                weekday_stats[weekday]["avg_l3"].append(l3)
                
                # Period statistics
                if 0 <= hour < 6:
                    period = "night"
                elif 6 <= hour < 12:
                    period = "morning"
                elif 12 <= hour < 18:
                    period = "afternoon"
                else:
                    period = "evening"
                
                period_stats[period]["count"] += 1
                period_stats[period]["max_l1"] = max(period_stats[period]["max_l1"], l1)
                period_stats[period]["max_l2"] = max(period_stats[period]["max_l2"], l2)
                period_stats[period]["max_l3"] = max(period_stats[period]["max_l3"], l3)
                if max_phase > PHASE_LIMIT_W * 0.80:  # Only track significant peaks
                    period_stats[period]["peaks"].append({
                        "timestamp": entry.get("timestamp"),
                        "max_phase_w": max_phase,
                        "l1_w": l1,
                        "l2_w": l2,
                        "l3_w": l3
                    })
                
            except Exception as e:
                logger.debug(f"Error analyzing entry: {e}")
                continue
        
        # Calculate averages and format results
        hour_analysis = []
        for h in range(24):
            stats = hour_stats[h]
            if stats["count"] > 0:
                hour_analysis.append({
                    "hour": h,
                    "time_label": f"{h:02d}:00-{h:02d}:59",
                    "measurements": stats["count"],
                    "max_l1_w": int(stats["max_l1"]),
                    "max_l2_w": int(stats["max_l2"]),
                    "max_l3_w": int(stats["max_l3"]),
                    "avg_l1_w": int(sum(stats["avg_l1"]) / len(stats["avg_l1"])) if stats["avg_l1"] else 0,
                    "avg_l2_w": int(sum(stats["avg_l2"]) / len(stats["avg_l2"])) if stats["avg_l2"] else 0,
                    "avg_l3_w": int(sum(stats["avg_l3"]) / len(stats["avg_l3"])) if stats["avg_l3"] else 0,
                    "max_overall_w": max(stats["max_l1"], stats["max_l2"], stats["max_l3"])
                })
        
        # Sort by max overall
        hour_analysis.sort(key=lambda x: x["max_overall_w"], reverse=True)
        
        weekday_names = ["Maandag", "Dinsdag", "Woensdag", "Donderdag", "Vrijdag", "Zaterdag", "Zondag"]
        weekday_analysis = []
        for d in range(7):
            stats = weekday_stats[d]
            if stats["count"] > 0:
                weekday_analysis.append({
                    "weekday": d,
                    "name": weekday_names[d],
                    "measurements": stats["count"],
                    "max_l1_w": int(stats["max_l1"]),
                    "max_l2_w": int(stats["max_l2"]),
                    "max_l3_w": int(stats["max_l3"]),
                    "avg_l1_w": int(sum(stats["avg_l1"]) / len(stats["avg_l1"])) if stats["avg_l1"] else 0,
                    "avg_l2_w": int(sum(stats["avg_l2"]) / len(stats["avg_l2"])) if stats["avg_l2"] else 0,
                    "avg_l3_w": int(sum(stats["avg_l3"]) / len(stats["avg_l3"])) if stats["avg_l3"] else 0,
                    "max_overall_w": max(stats["max_l1"], stats["max_l2"], stats["max_l3"])
                })
        
        # Sort by max overall
        weekday_analysis.sort(key=lambda x: x["max_overall_w"], reverse=True)
        
        # Period analysis
        period_names = {
            "night": "Nacht (00:00-06:00)",
            "morning": "Ochtend (06:00-12:00)",
            "afternoon": "Middag (12:00-18:00)",
            "evening": "Avond (18:00-24:00)"
        }
        period_analysis = []
        for period, stats in period_stats.items():
            if stats["count"] > 0:
                period_analysis.append({
                    "period": period,
                    "name": period_names[period],
                    "measurements": stats["count"],
                    "max_l1_w": int(stats["max_l1"]),
                    "max_l2_w": int(stats["max_l2"]),
                    "max_l3_w": int(stats["max_l3"]),
                    "max_overall_w": max(stats["max_l1"], stats["max_l2"], stats["max_l3"]),
                    "significant_peaks": len(stats["peaks"]),
                    "top_peaks": sorted(stats["peaks"], key=lambda x: x["max_phase_w"], reverse=True)[:5]
                })
        
        # Sort by max overall
        period_analysis.sort(key=lambda x: x["max_overall_w"], reverse=True)
        
        # Find peak hours (top 5)
        peak_hours = hour_analysis[:5]
        
        # Find risky hours (>80% of limit on average)
        risky_threshold = PHASE_LIMIT_W * 0.80
        risky_hours = [h for h in hour_analysis if max(h["avg_l1_w"], h["avg_l2_w"], h["avg_l3_w"]) > risky_threshold]
        
        return {
            "data_filter": {
                "description": "Alleen metingen ZONDER Zappi laden - Zappi zou zich aanpassen aan 3x25A",
                "total_measurements": total_entries,
                "used_measurements": filtered_count,
                "excluded_zappi_measurements": excluded_count,
                "exclusion_rate_percent": round((excluded_count / total_entries) * 100, 1) if total_entries > 0 else 0
            },
            "summary": {
                "total_measurements": filtered_count,
                "date_range": {
                    "from": filtered_entries[0].get("timestamp") if filtered_entries else None,
                    "to": filtered_entries[-1].get("timestamp") if filtered_entries else None
                },
                "peak_hours_of_day": [f"{h['hour']:02d}:00" for h in peak_hours],
                "riskiest_day": weekday_analysis[0]["name"] if weekday_analysis else None,
                "riskiest_period": period_analysis[0]["name"] if period_analysis else None
            },
            "by_hour": hour_analysis,
            "by_weekday": weekday_analysis,
            "by_period": period_analysis,
            "risky_hours": risky_hours,
            "recommendations": self._generate_recommendations(hour_analysis, weekday_analysis, period_analysis),
            "generated_at": datetime.now().isoformat()
        }
    
    def _generate_recommendations(self, hours, weekdays, periods) -> List[str]:
        """Genereer aanbevelingen op basis van patronen (ZONDER Zappi data)"""
        recommendations = []
        
        # Check peak hours
        if hours:
            top_hour = hours[0]
            if top_hour["max_overall_w"] > PHASE_LIMIT_W:
                recommendations.append(f"⚠️ Hoogste piek om {top_hour['time_label']} ({top_hour['max_overall_w']}W) - vermijd grote apparaten op dit tijdstip")
            elif top_hour["max_overall_w"] > PHASE_LIMIT_W * 0.90:
                recommendations.append(f"⚡ Let op: om {top_hour['time_label']} wordt het vaak krap ({top_hour['max_overall_w']}W)")
        
        # Check evening period
        evening = next((p for p in periods if p["period"] == "evening"), None)
        if evening and evening["max_overall_w"] > PHASE_LIMIT_W * 0.85:
            recommendations.append("🌙 Avond (18-24u) is kritieke periode - spreid verbruik waar mogelijk")
        
        # Check morning period
        morning = next((p for p in periods if p["period"] == "morning"), None)
        if morning and morning["max_overall_w"] > PHASE_LIMIT_W * 0.85:
            recommendations.append("☀️ Ochtend (06-12u) heeft ook pieken - let op bij meerdere apparaten tegelijk")
        
        # Weekend vs weekday
        if weekdays:
            weekend = [d for d in weekdays if d["weekday"] in [5, 6]]
            weekday = [d for d in weekdays if d["weekday"] not in [5, 6]]
            if weekend and weekday:
                weekend_max = max(d["max_overall_w"] for d in weekend)
                weekday_max = max(d["max_overall_w"] for d in weekday)
                if weekend_max > weekday_max + 500:
                    recommendations.append("📅 Weekend heeft hogere pieken - extra alert op zaterdag/zondag")
                elif weekday_max > weekend_max + 500:
                    recommendations.append("💼 Doordeweeks hogere pieken - waarschijnlijk thuiswerken/ochtendspits")
        
        # Check for consistent high usage
        high_avg_hours = [h for h in hours if max(h["avg_l1_w"], h["avg_l2_w"], h["avg_l3_w"]) > PHASE_LIMIT_W * 0.70]
        if len(high_avg_hours) > 5:
            recommendations.append(f"📊 {len(high_avg_hours)} uren met gemiddeld >70% belasting - overweeg spreiding van vast verbruik")
        
        if not recommendations:
            recommendations.append("✅ Geen specifieke aanbevelingen - belasting lijkt goed gespreid")
        
        return recommendations
    
    def dismiss_violation(self, timestamp: str, reason: str) -> Dict:
        """Markeer een violation als "dismissed" met reden
        
        Deze violation blijft zichtbaar in logs maar telt NIET mee voor 3x25A analyse.
        Gebruik dit voor: batterij laden, test situaties, bewuste overschrijdingen.
        
        Args:
            timestamp: ISO timestamp van de violation
            reason: Reden voor dismiss (bijv. "Batterij laden vanaf grid")
        
        Returns:
            Success status en aangepaste violation entry
        """
        # Zoek violation in log
        found = False
        for entry in self.log:
            if entry.get("timestamp") == timestamp:
                entry["dismissed"] = True
                entry["dismiss_reason"] = reason
                entry["dismissed_at"] = datetime.now().isoformat()
                found = True
                break
        
        # Ook in permanent violations log
        for entry in self.violations_log:
            if entry.get("timestamp") == timestamp:
                entry["dismissed"] = True
                entry["dismiss_reason"] = reason
                entry["dismissed_at"] = datetime.now().isoformat()
                break
        
        if found:
            self._save_log()
            self._save_violations_log()
            logger.info(f"✅ Violation dismissed: {timestamp} - Reden: {reason}")
            return {"success": True, "message": f"Violation marked as dismissed: {reason}"}
        else:
            return {"success": False, "error": "Violation not found"}
    
    def bulk_dismiss_violations(self, 
                                 start_time: str = None, 
                                 end_time: str = None,
                                 max_overshoot_w: int = None,
                                 phase: str = None,
                                 reason: str = "Bulk dismiss") -> Dict:
        """Dismiss meerdere violations in één keer op basis van filters
        
        Args:
            start_time: ISO timestamp start (optioneel)
            end_time: ISO timestamp einde (optioneel)
            max_overshoot_w: Max overschrijding in Watt (bijv. 200 = dismiss alles <200W over limiet)
            phase: Specifieke fase (L1/L2/L3) of None voor alle fases
            reason: Reden voor dismiss
        
        Returns:
            Aantal dismissed violations
        """
        count = 0
        
        for entry in self.log:
            if entry.get("dismissed", False):
                continue  # Skip als al dismissed
            
            if not entry.get("is_violation", False):
                continue  # Skip als geen violation
            
            # Check time range
            if start_time and entry.get("timestamp") < start_time:
                continue
            if end_time and entry.get("timestamp") > end_time:
                continue
            
            # Check overshoot
            if max_overshoot_w is not None:
                violations = entry.get("violations", [])
                # Parse violation strings like "L3: 5146W (>5100W)"
                should_dismiss = True
                for v_str in violations:
                    # Extract actual value from violation string
                    parts = v_str.split(":")
                    if len(parts) > 1:
                        value_str = parts[1].strip().split("W")[0]
                        try:
                            actual_w = abs(int(value_str))
                            overshoot = actual_w - PHASE_LIMIT_W
                            if overshoot > max_overshoot_w:
                                should_dismiss = False
                                break
                        except:
                            pass
                if not should_dismiss:
                    continue
            
            # Check phase filter
            if phase:
                violations = entry.get("violations", [])
                if not any(phase in v for v in violations):
                    continue
            
            # Dismiss this violation
            entry["dismissed"] = True
            entry["dismiss_reason"] = reason
            entry["dismissed_at"] = datetime.now().isoformat()
            count += 1
        
        # Also update violations_log
        for entry in self.violations_log:
            if entry.get("dismissed", False):
                continue
            
            if not entry.get("is_violation", False):
                continue
            
            if start_time and entry.get("timestamp") < start_time:
                continue
            if end_time and entry.get("timestamp") > end_time:
                continue
            
            if max_overshoot_w is not None:
                violations = entry.get("violations", [])
                should_dismiss = True
                for v_str in violations:
                    parts = v_str.split(":")
                    if len(parts) > 1:
                        value_str = parts[1].strip().split("W")[0]
                        try:
                            actual_w = abs(int(value_str))
                            overshoot = actual_w - PHASE_LIMIT_W
                            if overshoot > max_overshoot_w:
                                should_dismiss = False
                                break
                        except:
                            pass
                if not should_dismiss:
                    continue
            
            if phase:
                violations = entry.get("violations", [])
                if not any(phase in v for v in violations):
                    continue
            
            entry["dismissed"] = True
            entry["dismiss_reason"] = reason
            entry["dismissed_at"] = datetime.now().isoformat()
        
        if count > 0:
            self._save_log()
            self._save_violations_log()
            logger.info(f"✅ Bulk dismissed {count} violations - Reden: {reason}")
        
        return {
            "success": True, 
            "count": count,
            "message": f"{count} violations dismissed with reason: {reason}"
        }
    
    def reset_peak_values(self) -> Dict:
        """Reset alleen de max waarden per fase (niet de metingen)
        
        Returns:
            Success status met oude waarden
        """
        old_values = {
            "max_l1": self.peak_values["max_l1"],
            "max_l2": self.peak_values["max_l2"],
            "max_l3": self.peak_values["max_l3"],
        }
        
        # Reset peak values
        self.peak_values = {
            "max_l1": 0,
            "max_l2": 0,
            "max_l3": 0,
            "max_l1_timestamp": None,
            "max_l2_timestamp": None,
            "max_l3_timestamp": None,
            "peak_history": {
                "l1": [],
                "l2": [],
                "l3": []
            }
        }
        
        # Reset stats
        self.stats = {
            "max_l1": 0,
            "max_l2": 0,
            "max_l3": 0,
        }
        
        # Save
        self._save_peak_values()
        
        logger.info(f"🔄 Peak values reset: L1={old_values['max_l1']}W, L2={old_values['max_l2']}W, L3={old_values['max_l3']}W")
        
        return {
            "success": True,
            "old_values": old_values,
            "message": f"Max waarden gereset: L1={old_values['max_l1']}W → 0W, L2={old_values['max_l2']}W → 0W, L3={old_values['max_l3']}W → 0W"
        }
    
    def reset_stats(self, keep_violations: bool = True) -> Dict:
        """Reset alle statistieken en data
        
        Args:
            keep_violations: Behoud violations_log (permanent log) of ook resetten
        
        Returns:
            Success status met info over wat is gereset
        """
        # Reset normale log
        old_log_count = len(self.log)
        self.log = []
        
        # Reset violations log (optioneel)
        old_violations_count = len(self.violations_log)
        if not keep_violations:
            self.violations_log = []
        
        # Reset peak values
        self.peak_values = {
            "max_l1": 0,
            "max_l2": 0,
            "max_l3": 0,
            "max_l1_timestamp": None,
            "max_l2_timestamp": None,
            "max_l3_timestamp": None,
            "peak_history": {
                "l1": [],
                "l2": [],
                "l3": []
            }
        }
        
        # Reset stats
        self.stats = {
            "max_l1": 0,
            "max_l2": 0,
            "max_l3": 0,
        }
        
        # Save to files
        self._save_log()
        if not keep_violations:
            self._save_violations_log()
        self._save_peak_values()
        
        logger.info(f"🔄 Stats reset: {old_log_count} log entries removed, {old_violations_count if not keep_violations else 0} violations removed")
        
        return {
            "success": True,
            "log_entries_removed": old_log_count,
            "violations_removed": old_violations_count if not keep_violations else 0,
            "violations_kept": old_violations_count if keep_violations else 0,
            "message": f"Stats reset! {old_log_count} metingen verwijderd" + (f", {old_violations_count} violations behouden" if keep_violations else "")
        }
    
    def undismiss_violation(self, timestamp: str) -> Dict:
        """Verwijder dismiss markering van een violation
        
        Args:
            timestamp: ISO timestamp van de violation
        
        Returns:
            Success status
        """
        found = False
        for entry in self.log:
            if entry.get("timestamp") == timestamp:
                entry["dismissed"] = False
                entry["dismiss_reason"] = None
                entry["dismissed_at"] = None
                found = True
                break
        
        for entry in self.violations_log:
            if entry.get("timestamp") == timestamp:
                entry["dismissed"] = False
                entry["dismiss_reason"] = None
                entry["dismissed_at"] = None
                break
        
        if found:
            self._save_log()
            self._save_violations_log()
            logger.info(f"✅ Violation undismissed: {timestamp}")
            return {"success": True, "message": "Violation dismiss removed"}
        else:
            return {"success": False, "error": "Violation not found"}
    
    def analyze_feasibility(self) -> Dict:
        """Analyseer of 3x25A haalbaar is (ZONDER Zappi load balancing violations EN dismissed violations)"""
        total = len(self.log)
        
        # BELANGRIJK: Alleen "echte" violations tellen (zonder Zappi laden EN zonder dismissed)
        real_violations = len([e for e in self.log if e.get("is_real_violation", False) and not e.get("dismissed", False)])
        zappi_violations = len([e for e in self.log if e.get("is_violation", False) and e.get("zappi_charging", False)])
        dismissed_violations = len([e for e in self.log if e.get("is_violation", False) and e.get("dismissed", False)])
        total_violations = len([e for e in self.log if e.get("is_violation", False)])
        
        if total == 0:
            return {
                "feasible": None,
                "message": "Onvoldoende data",
                "confidence": 0
            }
        
        # Bereken violation rate ALLEEN voor echte violations (zonder Zappi)
        real_violation_rate = (real_violations / total) * 100
        
        # Calculate per-phase statistics (alleen echte violations)
        l1_violations = sum(1 for e in self.log if e.get("is_real_violation") and any("L1" in v for v in e.get("violations", [])))
        l2_violations = sum(1 for e in self.log if e.get("is_real_violation") and any("L2" in v for v in e.get("violations", [])))
        l3_violations = sum(1 for e in self.log if e.get("is_real_violation") and any("L3" in v for v in e.get("violations", [])))
        
        feasible = real_violation_rate < 1.0  # < 1% echte overschrijdingen = OK
        confidence = min(100, (total / 17280) * 100)  # 17280 = 1 dag data @ 5s interval
        
        # Build recommendation with dismissed info
        rec_parts = []
        if feasible:
            rec_parts.append(f"✅ 3x25A is haalbaar")
            if zappi_violations > 0:
                rec_parts.append(f"({zappi_violations} violations tijdens Zappi laden)")
            if dismissed_violations > 0:
                rec_parts.append(f"({dismissed_violations} dismissed)")
        else:
            rec_parts.append(f"❌ 3x25A NIET veilig ({real_violations} echte violations)")
            if dismissed_violations > 0:
                rec_parts.append(f"({dismissed_violations} dismissed, niet meegeteld)")
        
        return {
            "feasible": feasible,
            "violation_rate_percent": round(real_violation_rate, 2),
            "total_measurements": total,
            "total_violations": total_violations,
            "real_violations": real_violations,
            "zappi_violations": zappi_violations,
            "dismissed_violations": dismissed_violations,
            "l1_violations": l1_violations,
            "l2_violations": l2_violations,
            "l3_violations": l3_violations,
            "confidence_percent": round(confidence, 1),
            "recommendation": " ".join(rec_parts),
            "max_recorded": {
                "l1_w": self.stats["max_l1"],
                "l2_w": self.stats["max_l2"],
                "l3_w": self.stats["max_l3"]
            }
        }
