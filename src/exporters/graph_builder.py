"""
In-Memory Knowledge Graph Builder using NetworkX.
Connects Startups -> Products, Papers -> Repositories, Jobs -> Companies.
Enforces graph connectivity architecture from PROJECT_CONTEXT.md Section 2.
"""

from typing import Any, Dict, List, Optional
import networkx as nx

from src.schemas.entities import (
    JobRecord,
    NewsRecord,
    ProductRecord,
    ResearchPaperRecord,
    StartupRecord,
)
from src.utils.logger import logger


from src.exporters.base import to_str


class KnowledgeGraphBuilder:
    """Constructs and analyzes the in-memory entity relationship graph."""

    def __init__(self):
        self.graph = nx.DiGraph()

    def _add_startup(self, name: str, source_url: str = "") -> str:
        s_node = f"Startup:{name}"
        if s_node not in self.graph:
            self.graph.add_node(s_node, label=name, node_type="STARTUP", source=source_url)
        return s_node

    def build_graph(
        self,
        startups: Optional[List[StartupRecord]] = None,
        products: Optional[List[ProductRecord]] = None,
        papers: Optional[List[ResearchPaperRecord]] = None,
        jobs: Optional[List[JobRecord]] = None,
        news: Optional[List[NewsRecord]] = None,
    ) -> nx.DiGraph:
        """Populate graph with entities and their directed relationships."""
        self.graph.clear()

        # 1. Add Startups
        for s in (startups or []):
            self._add_startup(s.content.entityName, s.source.url)

        # 2. Add Products & Edge: Startup -> SHIPPED -> Product
        for p in (products or []):
            s_node = self._add_startup(p.content.startupName)
            p_node = f"Product:{p.content.productName}"
            self.graph.add_node(p_node, label=p.content.productName, node_type="PRODUCT", pricing=to_str(p.content.pricingModel))
            self.graph.add_edge(s_node, p_node, relationship="SHIPPED")

        # 3. Add Research Papers & GitHub Repos: Paper -> IMPLEMENTS -> Repo
        for r in (papers or []):
            paper_node = f"Paper:{r.content.paper_url}"
            self.graph.add_node(paper_node, label=r.content.title, node_type="RESEARCH_PAPER", paper_url=r.content.paper_url)
            if r.content.github_url:
                repo_node = f"Repo:{r.content.github_url}"
                self.graph.add_node(repo_node, label=r.content.github_url, node_type="GITHUB_REPO", stars=r.content.github_stars or 0)
                self.graph.add_edge(paper_node, repo_node, relationship="IMPLEMENTED_IN")

        # 4. Add Jobs & Edge: Startup -> HIRED_VIA -> Job
        for j in (jobs or []):
            s_node = self._add_startup(j.content.company)
            j_title = f"{j.content.company} - {j.content.title}"
            j_node = f"Job:{j_title}"
            self.graph.add_node(j_node, label=j_title, node_type="JOB", role_family=to_str(j.content.role_family))
            self.graph.add_edge(s_node, j_node, relationship="HIRED_VIA")

        logger.info(f"Knowledge Graph constructed: {self.graph.number_of_nodes()} nodes, {self.graph.number_of_edges()} edges.")
        return self.graph

    def get_summary_metrics(self) -> Dict[str, Any]:
        """Compute summary statistics for the current graph."""
        from collections import Counter
        nodes_by_type = dict(Counter(attrs.get("node_type", "UNKNOWN") for _, attrs in self.graph.nodes(data=True)))
        sorted_hubs = sorted(self.graph.degree(), key=lambda x: x[1], reverse=True)[:5]
        return {
            "total_nodes": self.graph.number_of_nodes(),
            "total_edges": self.graph.number_of_edges(),
            "nodes_by_type": nodes_by_type,
            "top_connected_hubs": [{"node": node, "connections": deg} for node, deg in sorted_hubs],
        }
