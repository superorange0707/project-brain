from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .core import BrainError


REPOSITORIES: dict[str, dict[str, str]] = {
    "customer-service": {
        "pom.xml": '<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion><groupId>demo</groupId><artifactId>customer-service</artifactId><version>1.0.0</version></project>\n',
        "src/main/java/demo/CustomerPublisher.java": '''package demo;
class CustomerPublisher {
    void publish(CustomerEvent event) {
        kafkaTemplate.send("customer.updated", event);
    }
}
''',
        "src/main/java/demo/CustomerEvent.java": '''package demo;
record CustomerEvent(Type type, String customerId) {
    enum Type { ADDRESS_CHANGED, JURISDICTION_CHANGED }
}
''',
    },
    "trading-service": {
        "pom.xml": '''<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion><groupId>demo</groupId><artifactId>trading-service</artifactId><version>1.0.0</version><dependencies><dependency><groupId>demo</groupId><artifactId>risk-client</artifactId><version>1.0.0</version></dependency></dependencies></project>
''',
        "src/main/java/demo/CustomerChangedListener.java": '''package demo;
class CustomerChangedListener {
    private final TradingEligibilityService eligibility;

    @KafkaListener(topics = "${topics.customer}")
    void handle(CustomerEvent event) {
        if (event.type() == CustomerEvent.Type.ADDRESS_CHANGED) {
            eligibility.recalculate(event.customerId());
        }
        // Bug: jurisdiction changes are ignored until the nightly batch.
    }
}
''',
        "src/main/java/demo/EligibilityEvaluator.java": '''package demo;
interface EligibilityEvaluator { void recalculate(String customerId); }
''',
        "src/main/java/demo/TradingEligibilityService.java": '''package demo;
@Service
class TradingEligibilityService implements EligibilityEvaluator {
    private final RiskClient riskClient;
    public void recalculate(String customerId) {
        riskClient.getRestrictions(customerId);
        save(customerId);
    }
    private void save(String customerId) {}
}
''',
        "src/main/java/demo/RiskClient.java": '''package demo;
@FeignClient(name = "${services.risk}", path = "/risk")
interface RiskClient {
    @GetMapping("/restrictions/{id}") Object getRestrictions(String id);
}
''',
        "src/main/resources/application.properties": "topics.customer=customer.updated\nservices.risk=risk-service\n",
        "src/test/java/demo/CustomerChangedListenerTest.java": '''package demo;
class CustomerChangedListenerTest {
    void addressChangeRecalculates() { eligibility.recalculate("customer-1"); }
    // Missing regression coverage for JURISDICTION_CHANGED.
}
''',
    },
    "risk-service": {
        "pom.xml": '<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion><groupId>demo</groupId><artifactId>risk-client</artifactId><version>1.0.0</version></project>\n',
        "src/main/java/demo/RiskController.java": '''package demo;
@RestController
@RequestMapping("/risk")
class RiskController {
    @GetMapping("/restrictions/{customerId}")
    Object restrictions(String customerId) { return null; }
}
''',
    },
    "batch-service": {
        "pom.xml": '<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion><groupId>demo</groupId><artifactId>batch-service</artifactId><version>1.0.0</version></project>\n',
        "src/main/java/demo/NightlyEligibilityJob.java": '''package demo;
@Scheduled(cron = "0 0 0 * * *")
class NightlyEligibilityJob { void refreshAllCustomers() {} }
''',
    },
}


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _init_git(path: Path) -> None:
    if not shutil.which("git"):
        return
    initialized = subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, capture_output=True, check=False)
    if initialized.returncode != 0:
        subprocess.run(["git", "init", "-q"], cwd=path, capture_output=True, check=False)
    for key, value in (("user.name", "Project Brain Demo"), ("user.email", "demo@example.invalid")):
        subprocess.run(["git", "config", key, value], cwd=path, capture_output=True, check=False)
    subprocess.run(["git", "add", "."], cwd=path, capture_output=True, check=False)
    subprocess.run(["git", "commit", "-q", "-m", "Create Project Brain demo"], cwd=path, capture_output=True, check=False)


def create_demo(target: Path) -> Path:
    target = target.expanduser().resolve()
    if target.exists() and any(target.iterdir()):
        raise BrainError(f"Demo target is not empty: {target}")
    target.mkdir(parents=True, exist_ok=True)
    for repo_name, files in REPOSITORIES.items():
        repo = target / repo_name
        repo.mkdir()
        for relative, content in files.items():
            _write(repo / relative, content)
        _init_git(repo)

    rows = [
        "[project]",
        'name = "project-brain-demo"',
        'runs_dir = ".runs"',
        'state_dir = "state"',
        'generated_dir = "generated"',
        "",
        "[knowledge]",
        'path = "knowledge"',
        "",
        "[graph]",
        "enabled = false",
    ]
    for repo_name in REPOSITORIES:
        rows.extend(["", "[[repositories]]", f'name = "{repo_name}"', f'path = "{repo_name}"'])
    config = target / "brain.toml"
    _write(config, "\n".join(rows) + "\n")
    _write(
        target / "ticket.md",
        """# DEMO-101 — Jurisdiction changes refresh only overnight

When a customer's jurisdiction changes, trading eligibility is not recalculated
until the nightly batch. Address changes already recalculate immediately.

## Acceptance criteria

- Jurisdiction changes trigger the existing online recalculation flow.
- Address-change behaviour remains unchanged.
- Add regression coverage.
- Do not change the Kafka topic or Risk REST contract.
""",
    )
    _write(
        target / "knowledge/PROJECT_MAP.md",
        "# Project Map\n\nCustomer Service publishes customer events. Trading owns eligibility. Risk owns restrictions. Batch provides the nightly fallback.\n",
    )
    _write(target / "knowledge/glossary.md", "# Glossary\n\nJurisdiction is the regulatory country used by trading eligibility.\n")
    _write(target / ".gitignore", ".runs/\nstate/\ngenerated/\n")
    return config
