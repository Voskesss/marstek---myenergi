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
MONITOR_INTERVAL_S = 5  # Check elke 5 seconden
MAX_LOG_ENTRIES = 10000  # Bewaar max 10k entries (ca. 2 dagen)


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
        
        # Load existing log
        self.log = self._load_log()
    
    def _load_log(self) -> List[Dict]:
        """Laad bestaande log entries"""
        try:
            if LOG_FILE.exists():
                with open(LOG_FILE, 'r') as f:
                    return json.load(f)
        except Exception as e:
            logger.warning(f"Could not load phase log: {e}")
        return []
    
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
                        ectp4 = zappi.get("ectp4")  # Grid L1
                        ectp5 = zappi.get("ectp5")  # Grid L2
                        ectp6 = zappi.get("ectp6")  # Grid L3
                        
                        # If Zappi has grid CT data, use it!
                        if ectp4 is not None or ectp5 is not None or ectp6 is not None:
                            logger.debug(f"Using Zappi Grid CT: L1={ectp4}W L2={ectp5}W L3={ectp6}W")
                            return {
                                "l1_w": int(ectp4) if ectp4 is not None else 0,
                                "l2_w": int(ectp5) if ectp5 is not None else 0,
                                "l3_w": int(ectp6) if ectp6 is not None else 0,
                                "source": "Zappi Grid CT (ectp4/5/6)"
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
    
    def _update_stats(self, phases: Dict, violations: List[str]):
        """Update statistieken"""
        self.stats["total_checks"] += 1
        
        l1 = phases.get("l1_w", 0)
        l2 = phases.get("l2_w", 0)
        l3 = phases.get("l3_w", 0)
        
        # Track maximums
        if abs(l1) > abs(self.stats["max_l1"]):
            self.stats["max_l1"] = l1
        if abs(l2) > abs(self.stats["max_l2"]):
            self.stats["max_l2"] = l2
        if abs(l3) > abs(self.stats["max_l3"]):
            self.stats["max_l3"] = l3
        
        # Track violations
        if violations:
            self.stats["violations_count"] += 1
            self.stats["last_violation"] = datetime.now().isoformat()
    
    def _log_entry(self, phases: Dict, violations: List[str]):
        """Log entry (alleen als er overschrijding is OF elke 5 minuten)"""
        now = datetime.now()
        
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
                "violations": violations,
                "is_violation": len(violations) > 0
            }
            self.log.append(entry)
            self._save_log()
            
            if violations:
                logger.warning(f"⚠️ FASE OVERSCHRIJDING: {', '.join(violations)}")
    
    async def _monitor_loop(self):
        """Main monitoring loop"""
        logger.info(f"🔌 Fase monitor gestart - Limiet: {PHASE_LIMIT_W}W per fase")
        self.stats["started_at"] = datetime.now().isoformat()
        
        while self.running:
            try:
                # Read phase data
                phases = await self._read_phases()
                
                if phases and any(v is not None for v in phases.values()):
                    # Check for violations
                    violations = self._check_violations(phases)
                    
                    # Update statistics
                    self._update_stats(phases, violations)
                    
                    # Log if needed
                    self._log_entry(phases, violations)
                
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
        """Haal huidige statistieken op"""
        return {
            **self.stats,
            "limit_per_phase_w": PHASE_LIMIT_W,
            "log_entries": len(self.log),
            "running": self.running
        }
    
    def get_violations(self, hours: int = 24) -> List[Dict]:
        """Haal overschrijdingen op van laatste X uur"""
        cutoff = datetime.now() - timedelta(hours=hours)
        
        violations = [
            entry for entry in self.log
            if entry.get("is_violation") and 
            datetime.fromisoformat(entry["timestamp"]) > cutoff
        ]
        
        return violations
    
    def get_recent_data(self, count: int = 100) -> List[Dict]:
        """Haal laatste N entries op"""
        return self.log[-count:] if len(self.log) > count else self.log
    
    def analyze_feasibility(self) -> Dict:
        """Analyseer of 3x25A haalbaar is"""
        total = len([e for e in self.log if not e.get("is_violation")])
        violations = len([e for e in self.log if e.get("is_violation")])
        
        if total == 0:
            return {
                "feasible": None,
                "message": "Onvoldoende data",
                "confidence": 0
            }
        
        violation_rate = violations / (total + violations) * 100
        
        # Calculate per-phase statistics
        l1_violations = sum(1 for e in self.log if e.get("is_violation") and any("L1" in v for v in e.get("violations", [])))
        l2_violations = sum(1 for e in self.log if e.get("is_violation") and any("L2" in v for v in e.get("violations", [])))
        l3_violations = sum(1 for e in self.log if e.get("is_violation") and any("L3" in v for v in e.get("violations", [])))
        
        feasible = violation_rate < 1.0  # < 1% overschrijdingen = OK
        confidence = min(100, (total / 17280) * 100)  # 17280 = 1 dag data @ 5s interval
        
        return {
            "feasible": feasible,
            "violation_rate_percent": round(violation_rate, 2),
            "total_measurements": total + violations,
            "total_violations": violations,
            "l1_violations": l1_violations,
            "l2_violations": l2_violations,
            "l3_violations": l3_violations,
            "confidence_percent": round(confidence, 1),
            "recommendation": "✅ 3x25A is haalbaar" if feasible else "❌ 3x25A NIET veilig - blijf bij 3x40A",
            "max_recorded": {
                "l1_w": self.stats["max_l1"],
                "l2_w": self.stats["max_l2"],
                "l3_w": self.stats["max_l3"]
            }
        }
