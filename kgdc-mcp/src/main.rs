//! kgdc-mcp — stateful MCP server for divide-and-conquer KG extraction.
//!
//! One shared graph per run. Agents add Turtle through `add_triples`; every
//! triple passes a vocabulary gate (declared class/property, range kind,
//! task scope, IRI syntax, per-task subject cap) before it lands. SHACL
//! (shacl-rust) validates the whole graph on demand. The orchestrator creates
//! tasks, reads notes and decides what to rerun. Nothing gets in that the
//! vocabulary does not allow.
//!
//!   kgdc-mcp --ontology onto.ttl --shapes shapes.ttl [--ns http://example.org/data/] [--max-subjects 40]
use std::collections::{HashMap, HashSet};
use std::sync::{Arc, Mutex};

use anyhow::{anyhow, Context, Result};
use oxrdf::vocab::{rdf, rdfs};
use oxrdf::{Graph, NamedNode, NamedNodeRef, NamedOrBlankNodeRef, TermRef, Triple, TripleRef};
use rmcp::handler::server::wrapper::{Json, Parameters};
use rmcp::{tool, tool_router, ServiceExt};
use serde::{Deserialize, Serialize};

const OWL_CLASS: &str = "http://www.w3.org/2002/07/owl#Class";
const RDFS_CLASS: &str = "http://www.w3.org/2000/01/rdf-schema#Class";
const XSD: &str = "http://www.w3.org/2001/XMLSchema#";
const RDF_NS: &str = "http://www.w3.org/1999/02/22-rdf-syntax-ns#";
const RDFS_NS: &str = "http://www.w3.org/2000/01/rdf-schema#";
const PROPERTY_TYPES: [&str; 5] = [
    "http://www.w3.org/1999/02/22-rdf-syntax-ns#Property",
    "http://www.w3.org/2000/01/rdf-schema#Property",
    "http://www.w3.org/2002/07/owl#ObjectProperty",
    "http://www.w3.org/2002/07/owl#DatatypeProperty",
    "http://www.w3.org/2002/07/owl#AnnotationProperty",
];

// ---------------------------------------------------------------------------
// Vocabulary
// ---------------------------------------------------------------------------
struct Vocab {
    ontology: Graph,
    shapes: Graph,
    prefixes: Vec<(String, String)>,
    ns: String,
    classes: HashMap<String, String>,      // iri -> "label — comment"
    properties: HashMap<String, PropInfo>, // iri -> info
    parents: HashMap<String, Vec<String>>, // class -> direct superclasses
}

#[derive(Clone, Default)]
struct PropInfo {
    domains: Vec<String>,
    ranges: Vec<String>,
    datatype: bool, // range is an xsd datatype
    comment: String,
}

fn lit_value(g: &Graph, s: NamedOrBlankNodeRef, p: NamedNodeRef) -> String {
    g.objects_for_subject_predicate(s, p)
        .find_map(|o| match o { TermRef::Literal(l) => Some(l.value().to_string()), _ => None })
        .unwrap_or_default()
}

fn iri_objects(g: &Graph, s: NamedOrBlankNodeRef, p: NamedNodeRef) -> Vec<String> {
    g.objects_for_subject_predicate(s, p)
        .filter_map(|o| match o { TermRef::NamedNode(n) => Some(n.as_str().to_string()), _ => None })
        .collect()
}

impl Vocab {
    fn load(onto_path: &str, shapes_path: &str, ns: String) -> Result<Self> {
        let (ontology, mut prefixes) = parse_turtle_file(onto_path)?;
        let (shapes, p2) = parse_turtle_file(shapes_path)?;
        prefixes.extend(p2);
        // Reserved names always mean the run's canonical namespaces (a shapes file
        // declaring its own `ex:` must not redirect where agents mint subjects).
        let reserved = [("ex", ns.as_str()), ("rdf", RDF_NS), ("rdfs", RDFS_NS), ("xsd", XSD)];
        prefixes.retain(|(p, iri)| !reserved.iter().any(|(rp, riri)| p == rp || iri == riri));
        // named prefix wins over the empty default for the same namespace; one prefix per namespace
        prefixes.sort_by_key(|(p, _)| p.is_empty());
        let mut seen = HashSet::new();
        prefixes.retain(|(p, iri)| !p.is_empty() && seen.insert(iri.clone()));
        prefixes.extend(reserved.iter().map(|(p, i)| (p.to_string(), i.to_string())));
        let mut classes = HashMap::new();
        let mut properties: HashMap<String, PropInfo> = HashMap::new();
        let mut parents = HashMap::new();
        for t in ontology.iter() {
            if t.predicate != rdf::TYPE { continue; }
            let (NamedOrBlankNodeRef::NamedNode(s), TermRef::NamedNode(ty)) = (t.subject, t.object) else { continue };
            let ty = ty.as_str();
            if ty == OWL_CLASS || ty == RDFS_CLASS {
                let desc = format!("{} — {}", lit_value(&ontology, t.subject, rdfs::LABEL), lit_value(&ontology, t.subject, rdfs::COMMENT));
                classes.insert(s.as_str().to_string(), desc);
                parents.insert(s.as_str().to_string(), iri_objects(&ontology, t.subject, rdfs::SUB_CLASS_OF));
            } else if PROPERTY_TYPES.contains(&ty) {
                let ranges = iri_objects(&ontology, t.subject, rdfs::RANGE);
                let datatype = ranges.iter().any(|r| r.starts_with(XSD));
                properties.insert(s.as_str().to_string(), PropInfo {
                    domains: iri_objects(&ontology, t.subject, rdfs::DOMAIN), ranges, datatype,
                    comment: lit_value(&ontology, t.subject, rdfs::COMMENT),
                });
            }
        }
        // properties that only the shapes mention (sh:path) are legal too
        let sh_path = NamedNode::new_unchecked("http://www.w3.org/ns/shacl#path");
        for t in shapes.iter() {
            if t.predicate == sh_path.as_ref() {
                if let TermRef::NamedNode(p) = t.object {
                    if !p.as_str().starts_with(RDFS_NS) && !p.as_str().starts_with(RDF_NS) {
                        properties.entry(p.as_str().to_string()).or_default();
                    }
                }
            }
        }
        Ok(Vocab { ontology, shapes, prefixes, ns, classes, properties, parents })
    }

    fn ancestors(&self, cls: &str) -> HashSet<String> {
        let mut out = HashSet::new();
        let mut todo = vec![cls.to_string()];
        while let Some(c) = todo.pop() {
            if out.insert(c.clone()) {
                if let Some(ps) = self.parents.get(&c) { todo.extend(ps.iter().cloned()); }
            }
        }
        out
    }

    fn descendants(&self, cls: &str) -> HashSet<String> {
        self.classes.keys().filter(|c| self.ancestors(c).contains(cls)).cloned().collect()
    }

    /// Classes an instance of `cls` may link to: ranges of its (inherited) properties, and their subclasses.
    fn dependencies(&self, cls: &str) -> HashSet<String> {
        let doms = self.ancestors(cls);
        let mut out = HashSet::new();
        for info in self.properties.values() {
            if info.domains.iter().any(|d| doms.contains(d)) {
                for r in &info.ranges {
                    if self.classes.contains_key(r) { out.extend(self.descendants(r)); }
                }
            }
        }
        out.remove(cls);
        out
    }

    fn qname(&self, iri: &str) -> String {
        let best = self.prefixes.iter().filter(|(_, ns)| iri.starts_with(ns)).max_by_key(|(_, ns)| ns.len());
        match best { Some((p, ns)) => format!("{}:{}", p, &iri[ns.len()..]), None => format!("<{iri}>") }
    }

    fn expand(&self, q: &str) -> String {
        let q = q.trim().trim_matches(|c| c == '<' || c == '>');
        if q.starts_with("http") { return q.to_string(); }
        if let Some((p, l)) = q.split_once(':') {
            if let Some((_, ns)) = self.prefixes.iter().find(|(px, _)| px == p) { return format!("{ns}{l}"); }
        }
        q.to_string()
    }

    fn prefix_block(&self) -> String {
        self.prefixes.iter().map(|(p, i)| format!("@prefix {p}: <{i}> .")).collect::<Vec<_>>().join("\n")
    }
}

fn parse_turtle_file(path: &str) -> Result<(Graph, Vec<(String, String)>)> {
    let bytes = std::fs::read(path).with_context(|| format!("reading {path}"))?;
    parse_turtle(&bytes, &[])
}

fn parse_turtle(bytes: &[u8], prefixes: &[(String, String)]) -> Result<(Graph, Vec<(String, String)>)> {
    let mut parser = oxttl::TurtleParser::new();
    for (p, iri) in prefixes { parser = parser.with_prefix(p, iri)?; }
    let mut reader = parser.for_slice(bytes);
    let mut g = Graph::new();
    for t in &mut reader { g.insert(&t?); }
    let px = reader.prefixes().map(|(p, i)| (p.to_string(), i.to_string())).collect();
    Ok((g, px))
}

fn local(iri: &str) -> &str { iri.rsplit(['/', '#']).next().unwrap_or(iri) }

// ---------------------------------------------------------------------------
// Run state
// ---------------------------------------------------------------------------
#[derive(Clone, Serialize, schemars::JsonSchema)]
struct Task {
    id: String,
    class: String, // qname
    text: String,
    context: String,
    status: String, // open | done | discarded
    hint: String,
    notes: Vec<String>,
    subjects: Vec<String>, // IRIs this task added triples for
    accepted: usize,
    rejected: usize,
}

struct State {
    vocab: Vocab,
    graph: Graph,
    tasks: Vec<Task>,
    issues: Vec<String>,
    max_subjects_per_task: usize,
}

#[derive(Clone)]
struct Server(Arc<Mutex<State>>);

#[derive(Deserialize, schemars::JsonSchema)]
struct ClassParam { #[serde(default)] class: Option<String> }
#[derive(Deserialize, schemars::JsonSchema)]
struct CreateTaskParam { class: String, text: String, #[serde(default)] context: String }
#[derive(Deserialize, schemars::JsonSchema)]
struct AddParam {
    task_id: String,
    /// Turtle statements, one per array item (e.g. "ex:p a chr:Person ." / "ex:p rdfs:label \"Ann\" ."). Short items are easier for small models than one long escaped string.
    #[serde(default)] lines: Vec<String>,
    /// Alternative: one Turtle snippet.
    #[serde(default)] turtle: String,
}
#[derive(Deserialize, schemars::JsonSchema)]
struct TripleParam { task_id: String, subject: String, predicate: String, object: String }
#[derive(Deserialize, schemars::JsonSchema)]
struct LookupParam { #[serde(default)] query: String, #[serde(default)] class: Option<String> }
#[derive(Deserialize, schemars::JsonSchema)]
struct TaskIdParam { #[serde(default)] task_id: Option<String> }
#[derive(Deserialize, schemars::JsonSchema)]
struct FinalizeParam { task_id: String, #[serde(default)] notes: Vec<String> }
#[derive(Deserialize, schemars::JsonSchema)]
struct ReopenParam { task_id: String, #[serde(default)] hint: String, #[serde(default)] discard_triples: bool }
#[derive(Deserialize, schemars::JsonSchema)]
struct TextParam { text: String }
#[derive(Deserialize, schemars::JsonSchema)]
struct NoParam {}

#[derive(Serialize, schemars::JsonSchema)]
struct AddResult { accepted: usize, rejected: Vec<String>, subjects_in_task: usize, violations_for_task: Vec<String> }
#[derive(Clone, Serialize, schemars::JsonSchema)]
struct Violation { focus: String, path: String, message: String, severity: String }
#[derive(Serialize, schemars::JsonSchema)]
struct Status { triples: usize, conforms: bool, violations: usize, tasks: Vec<Task>, issues: Vec<String>, build_order: Vec<Vec<String>> }

#[tool_router(server_handler)]
impl Server {
    #[tool(description = "The vocabulary an agent may use: prefixes, classes, properties (domain -> range) and one Turtle template per class. With `class` (qname), only what an instance of that class needs: its (inherited) properties, the classes they link to, their templates.")]
    fn vocabulary(&self, Parameters(ClassParam { class }): Parameters<ClassParam>) -> String {
        let st = self.0.lock().unwrap();
        let v = &st.vocab;
        let wanted: HashSet<String> = match class.map(|c| v.expand(&c)) {
            Some(c) if v.classes.contains_key(&c) => {
                let mut w: HashSet<String> = HashSet::from([c]);
                for _ in 0..2 { for x in w.clone() { w.extend(v.dependencies(&x)); } }
                w
            }
            _ => v.classes.keys().cloned().collect(),
        };
        let mut cls: Vec<&String> = wanted.iter().collect();
        cls.sort();
        let doms: HashSet<String> = wanted.iter().flat_map(|c| v.ancestors(c)).collect();
        let mut props: Vec<(&String, &PropInfo)> = v.properties.iter()
            .filter(|(_, i)| i.domains.is_empty() || i.domains.iter().any(|d| doms.contains(d))).collect();
        props.sort_by(|a, b| a.0.cmp(b.0));
        let names = |xs: &Vec<String>| if xs.is_empty() { "?".to_string() } else { xs.iter().map(|d| v.qname(d)).collect::<Vec<_>>().join(" | ") };

        let mut out = vec![v.prefix_block(), String::new(), "CLASSES:".into()];
        for c in &cls { out.push(format!("- {}: {}", v.qname(c), v.classes[*c])); }
        out.push(String::new());
        out.push("PROPERTIES (domain -> range):".into());
        for (p, i) in &props {
            out.push(format!("- {} ({} -> {}){}", v.qname(p), names(&i.domains), names(&i.ranges),
                if i.comment.is_empty() { String::new() } else { format!(": {}", i.comment) }));
        }
        out.push(String::new());
        out.push("TEMPLATES (replace ex:Class_1 with an IRI named from the text; fill only slots the text supports):".into());
        for c in &cls {
            let anc = v.ancestors(c);
            let mut lines = vec![format!("ex:{}_1 a {} ;", local(c), v.qname(c)), "    rdfs:label \"...\" ;".into()];
            for (p, i) in &props {
                if !i.domains.is_empty() && !i.domains.iter().any(|d| anc.contains(d)) { continue; }
                let ph = match i.ranges.first() {
                    Some(r) if r.starts_with(XSD) => format!("\"...\"^^{}", v.qname(r)),
                    Some(r) if v.classes.contains_key(r) => format!("ex:{}_1", local(r)),
                    _ => "<https://example.org/replace-with-the-real-iri>".into(),
                };
                lines.push(format!("    {} {} ;", v.qname(p), ph));
            }
            let mut block = lines.join("\n");
            block.pop();
            block.push('.');
            out.push(block);
            out.push(String::new());
        }
        out.join("\n")
    }

    #[tool(description = "Orchestrator: register a scoped extraction job. `class` = qname the agent must build, `text` = the segment, `context` = shared facts. Returns the task.")]
    fn create_task(&self, Parameters(CreateTaskParam { class, text, context }): Parameters<CreateTaskParam>) -> Result<Json<Task>, String> {
        let mut st = self.0.lock().unwrap();
        let iri = st.vocab.expand(&class);
        if !st.vocab.classes.contains_key(&iri) { return Err(format!("{class} is not a class of the vocabulary")); }
        let t = Task { id: format!("t{}", st.tasks.len() + 1), class: st.vocab.qname(&iri), text, context,
            status: "open".into(), hint: String::new(), notes: vec![], subjects: vec![], accepted: 0, rejected: 0 };
        st.tasks.push(t.clone());
        Ok(Json(t))
    }

    #[tool(description = "Agent: add triples to the shared graph under a task. Pass `lines`: an array of Turtle statements, ONE STATEMENT PER ITEM, each ending with ' .' (e.g. [\"ex:p a chr:Person .\", \"ex:p rdfs:label \\\"Ann\\\" .\"]); prefixes are predefined. A malformed item is rejected alone, the others still land. Each triple is gated: declared class/property only, object kind must match the property's range, subjects are ex: IRIs, rdf:type only within the task's scope (its class or classes it links to), no malformed IRIs, capped number of subjects per task. Rejected triples come back with the reason; accepted ones are in the graph. Also returns current SHACL violations on this task's subjects.")]
    fn add_triples(&self, Parameters(AddParam { task_id, lines, turtle }): Parameters<AddParam>) -> Result<Json<AddResult>, String> {
        let mut st = self.0.lock().unwrap();
        let ti = st.tasks.iter().position(|t| t.id == task_id).ok_or("unknown task")?;
        if st.tasks[ti].status != "open" { return Err(format!("task {task_id} is {}", st.tasks[ti].status)); }
        let mut items: Vec<String> = lines.into_iter().filter(|l| !l.trim().is_empty()).collect();
        if !turtle.trim().is_empty() { items.push(turtle); }
        if items.is_empty() { return Err("nothing to add: pass `lines` (one Turtle statement per item)".into()); }
        let mut g = Graph::new();
        let mut rejected = vec![];
        // Models split one block across items ("ex:m a chr:X ;", "rdfs:label ... ;", ...): try the
        // items as one document first; only if that fails parse each on its own so one bad
        // statement does not sink the batch.
        match parse_turtle(items.join("\n").as_bytes(), &st.vocab.prefixes) {
            Ok((gi, _)) => for t in gi.iter() { g.insert(t); },
            Err(_) => for item in &items {
                match parse_turtle(item.as_bytes(), &st.vocab.prefixes) {
                    Ok((gi, _)) => for t in gi.iter() { g.insert(t); },
                    Err(e) => rejected.push(format!("{} -> not valid Turtle: {}. Every statement starts with a subject IRI and ends with ' .'; local names use letters, digits, '_' and '-' only.",
                        item.chars().take(80).collect::<String>(), e.to_string().lines().next().unwrap_or(""))),
                }
            },
        }
        let task_cls = st.vocab.expand(&st.tasks[ti].class);
        let mut scope = st.vocab.dependencies(&task_cls);
        scope.insert(task_cls);
        let cap = st.max_subjects_per_task;
        let mut subjects: HashSet<String> = st.tasks[ti].subjects.iter().cloned().collect();
        let mut accepted = 0usize;
        let mut keep: Vec<Triple> = vec![];
        let batch_subjects: HashSet<String> = g.iter().filter_map(|t| match t.subject { NamedOrBlankNodeRef::NamedNode(n) => Some(n.as_str().to_string()), _ => None }).collect();
        for t in g.iter() {
            match gate(&st.vocab, &st.graph, &scope, t, &subjects, cap) {
                Ok(()) => {
                    if let TermRef::NamedNode(o) = t.object {   // vocabulary passed: the object must be a real individual
                        if o.as_str().starts_with(&st.vocab.ns) && t.predicate != rdf::TYPE
                            && !batch_subjects.contains(o.as_str()) && st.graph.triples_for_subject(o).next().is_none() {
                            rejected.push(format!("{} -> unknown individual {}: use lookup to find the existing IRI, or create it (with rdf:type) in the same call",
                                short_triple(&st.vocab, t), st.vocab.qname(o.as_str())));
                            continue;
                        }
                    }
                    if let NamedOrBlankNodeRef::NamedNode(s) = t.subject { subjects.insert(s.as_str().to_string()); }
                    keep.push(t.into_owned());
                    accepted += 1;
                }
                Err(why) => rejected.push(format!("{} -> {}", short_triple(&st.vocab, t), why)),
            }
        }
        for t in &keep { st.graph.insert(t); }
        let task = &mut st.tasks[ti];
        task.accepted += accepted;
        task.rejected += rejected.len();
        task.subjects = subjects.into_iter().collect();
        let subs = task.subjects.clone();
        let viol = shacl(&st).into_iter().filter(|v| subs.contains(&v.focus))
            .map(|v| format!("{} {}: {}", st.vocab.qname(&v.focus), v.path, v.message)).collect();
        Ok(Json(AddResult { accepted, rejected, subjects_in_task: subs.len(), violations_for_task: viol }))
    }

    #[tool(description = "Agent: add ONE triple. Terms as Turtle: subject \"ex:person_Ann\", predicate \"a\" or \"chr:hasCode\", object \"chr:Person\" / \"ex:unit_cm\" / \"<https://loinc.org/8302-2>\" / a literal like \"\\\"104.0\\\"^^xsd:float\". Same gate and response as add_triples.")]
    fn add_triple(&self, Parameters(TripleParam { task_id, subject, predicate, object }): Parameters<TripleParam>) -> Result<Json<AddResult>, String> {
        let (predicate, mut object) = (predicate.trim().to_string(), object.trim().to_string());
        // A bare value where a literal belongs: quote it, typed by the property's datatype range.
        let (is_qname, dt) = {
            let st = self.0.lock().unwrap();
            let is_qname = !object.contains(' ') && object.split_once(':').map_or(false, |(p, _)| st.vocab.prefixes.iter().any(|(px, _)| px == p));
            let p = st.vocab.expand(&predicate);
            let dt = st.vocab.properties.get(&p).and_then(|i| i.ranges.iter().find(|r| r.starts_with(XSD)).cloned()).map(|r| st.vocab.qname(&r));
            (is_qname, dt)
        };
        let looks_like_term = object.starts_with('<') || object.starts_with('"') || object.starts_with('\'') || object.starts_with("http") || is_qname;
        if !looks_like_term {
            object = match dt { Some(d) => format!("\"{}\"^^{}", object.replace('"', "\\\""), d), None => format!("\"{}\"", object.replace('"', "\\\"")) };
        } else if object.starts_with("http") {
            object = format!("<{object}>");
        }
        let line = format!("{} {} {} .", subject.trim(), predicate, object);
        self.add_triples(Parameters(AddParam { task_id, lines: vec![line], turtle: String::new() }))
    }

    #[tool(description = "Agent: find existing individuals to link to instead of minting new ones. `query` = case-insensitive substring of label or IRI; `class` (qname) restricts by type.")]
    fn lookup(&self, Parameters(LookupParam { query, class }): Parameters<LookupParam>) -> Json<Vec<String>> {
        let st = self.0.lock().unwrap();
        let cls = class.map(|c| st.vocab.expand(&c));
        let q = query.to_lowercase();
        let mut out = vec![];
        for t in st.graph.iter() {
            if t.predicate != rdf::TYPE { continue; }
            let (NamedOrBlankNodeRef::NamedNode(s), TermRef::NamedNode(ty)) = (t.subject, t.object) else { continue };
            if cls.as_deref().is_some_and(|c| c != ty.as_str()) { continue; }
            let label = lit_value(&st.graph, t.subject, rdfs::LABEL);
            if q.is_empty() || label.to_lowercase().contains(&q) || s.as_str().to_lowercase().contains(&q) {
                out.push(format!("{} a {} ; rdfs:label \"{}\"", st.vocab.qname(s.as_str()), st.vocab.qname(ty.as_str()), label));
            }
        }
        out.sort();
        Json(out)
    }

    #[tool(description = "SHACL validation of the shared graph (shacl-rust). With `task_id`, only violations whose focus node belongs to that task.")]
    fn validate(&self, Parameters(TaskIdParam { task_id }): Parameters<TaskIdParam>) -> Json<Vec<Violation>> {
        let st = self.0.lock().unwrap();
        let mut v = shacl(&st);
        if let Some(t) = task_id.and_then(|id| st.tasks.iter().find(|t| t.id == id)) { v.retain(|x| t.subjects.contains(&x.focus)); }
        for x in &mut v { x.focus = st.vocab.qname(&x.focus); }
        Json(v)
    }

    #[tool(description = "Agent: mark the task done. `notes` = what could not be filled from the text (UNRESOLVED), anything ambiguous, anything rejected that you believe is right. Returns the task's remaining violations.")]
    fn finalize(&self, Parameters(FinalizeParam { task_id, notes }): Parameters<FinalizeParam>) -> Result<Json<Vec<Violation>>, String> {
        let mut st = self.0.lock().unwrap();
        let ti = st.tasks.iter().position(|t| t.id == task_id).ok_or("unknown task")?;
        st.tasks[ti].status = "done".into();
        st.tasks[ti].notes.extend(notes);
        let subs = st.tasks[ti].subjects.clone();
        let mut v = shacl(&st);
        v.retain(|x| subs.contains(&x.focus));
        for x in &mut v { x.focus = st.vocab.qname(&x.focus); }
        Ok(Json(v))
    }

    #[tool(description = "Orchestrator: reopen a task with a hint for the next attempt; `discard_triples` first removes everything it added.")]
    fn reopen_task(&self, Parameters(ReopenParam { task_id, hint, discard_triples }): Parameters<ReopenParam>) -> Result<Json<Task>, String> {
        let mut st = self.0.lock().unwrap();
        let ti = st.tasks.iter().position(|t| t.id == task_id).ok_or("unknown task")?;
        if discard_triples { drop_task_triples(&mut st, ti); }
        st.tasks[ti].status = "open".into();
        st.tasks[ti].hint = hint;
        Ok(Json(st.tasks[ti].clone()))
    }

    #[tool(description = "Orchestrator: drop a task and every triple it added.")]
    fn discard_task(&self, Parameters(FinalizeParam { task_id, .. }): Parameters<FinalizeParam>) -> Result<String, String> {
        let mut st = self.0.lock().unwrap();
        let ti = st.tasks.iter().position(|t| t.id == task_id).ok_or("unknown task")?;
        let n = drop_task_triples(&mut st, ti);
        st.tasks[ti].status = "discarded".into();
        Ok(format!("discarded task {task_id}, removed {n} triples"))
    }

    #[tool(description = "Orchestrator: escalate something the pipeline cannot decide (ambiguity, contradiction, missing vocabulary) to the user.")]
    fn raise_issue(&self, Parameters(TextParam { text }): Parameters<TextParam>) -> String {
        let mut st = self.0.lock().unwrap();
        st.issues.push(text);
        format!("issue #{} recorded", st.issues.len())
    }

    #[tool(description = "Overall state: triple count, SHACL conformance, every task (status, notes, counts), open issues, and the bottom-up build order for `class` (comma-separated qnames; default: the classes of all tasks).")]
    fn status(&self, Parameters(ClassParam { class }): Parameters<ClassParam>) -> Json<Status> {
        let st = self.0.lock().unwrap();
        let v = shacl(&st);
        let classes: Vec<String> = match class {
            Some(c) => c.split(',').map(|x| st.vocab.expand(x)).collect(),
            None => st.tasks.iter().map(|t| st.vocab.expand(&t.class)).collect(),
        };
        let order = build_order(&st.vocab, &classes).into_iter().map(|l| l.into_iter().map(|c| st.vocab.qname(&c)).collect()).collect();
        Json(Status { triples: st.graph.len(), conforms: v.is_empty(), violations: v.len(), tasks: st.tasks.clone(), issues: st.issues.clone(), build_order: order })
    }

    #[tool(description = "The shared graph as Turtle.")]
    fn export(&self, Parameters(NoParam {}): Parameters<NoParam>) -> Result<String, String> {
        let st = self.0.lock().unwrap();
        let mut ser = oxttl::TurtleSerializer::new();
        for (p, i) in &st.vocab.prefixes { ser = ser.with_prefix(p, i).map_err(|e| e.to_string())?; }
        let mut w = ser.for_writer(Vec::new());
        for t in st.graph.iter() { w.serialize_triple(t).map_err(|e| e.to_string())?; }
        String::from_utf8(w.finish().map_err(|e| e.to_string())?).map_err(|e| e.to_string())
    }
}

// ---------------------------------------------------------------------------
// Gate, SHACL, helpers
// ---------------------------------------------------------------------------
fn gate(v: &Vocab, graph: &Graph, scope: &HashSet<String>, t: TripleRef, subjects: &HashSet<String>, cap: usize) -> std::result::Result<(), String> {
    let NamedOrBlankNodeRef::NamedNode(sn) = t.subject else { return Err("subject must be an IRI, not a blank node".into()) };
    let s = sn.as_str();
    if !s.starts_with(&v.ns) { return Err(format!("subjects must be minted in the ex: namespace ({})", v.ns)); }
    let obj_iri = match t.object { TermRef::NamedNode(n) => Some(n.as_str()), _ => None };
    for iri in [Some(s), Some(t.predicate.as_str()), obj_iri].into_iter().flatten() {
        if iri.chars().any(|c| c.is_whitespace() || "<>\"{}|\\^`".contains(c)) { return Err(format!("malformed IRI {iri}")); }
    }
    let p = t.predicate.as_str();
    if p == rdf::TYPE.as_str() {
        let TermRef::NamedNode(c) = t.object else { return Err("rdf:type must be an IRI".into()) };
        if !v.classes.contains_key(c.as_str()) { return Err(format!("{} is not a class of the vocabulary", v.qname(c.as_str()))); }
        if !scope.contains(c.as_str()) { return Err(format!("{} is outside this task's scope", v.qname(c.as_str()))); }
        let is_new = !subjects.contains(s) && graph.triples_for_subject(t.subject).next().is_none();
        if is_new && subjects.len() >= cap { return Err(format!("task already has {cap} subjects; finalize with a note instead of adding more")); }
        return Ok(());
    }
    if p == rdfs::LABEL.as_str() || p == rdfs::COMMENT.as_str() {
        return if matches!(t.object, TermRef::Literal(_)) { Ok(()) } else { Err("label/comment must be a literal".into()) };
    }
    let Some(info) = v.properties.get(p) else { return Err(format!("{} is not a property of the vocabulary", v.qname(p))) };
    let ranges = || info.ranges.iter().map(|r| v.qname(r)).collect::<Vec<_>>().join(" | ");
    match t.object {
        TermRef::Literal(_) if !info.datatype && !info.ranges.is_empty() => Err(format!("{} takes an IRI ({}), not a literal", v.qname(p), ranges())),
        TermRef::NamedNode(_) if info.datatype => Err(format!("{} takes a literal ({}), not an IRI", v.qname(p), ranges())),
        TermRef::BlankNode(_) => Err("blank nodes are not allowed".into()),
        _ => Ok(()),
    }
}

fn drop_task_triples(st: &mut State, ti: usize) -> usize {
    let subs: HashSet<String> = st.tasks[ti].subjects.drain(..).collect();
    let drop: Vec<Triple> = st.graph.iter()
        .filter(|t| matches!(t.subject, NamedOrBlankNodeRef::NamedNode(s) if subs.contains(s.as_str())))
        .map(|t| t.into_owned()).collect();
    for t in &drop { st.graph.remove(t); }
    st.tasks[ti].accepted = 0;
    drop.len()
}

fn shacl(st: &State) -> Vec<Violation> {
    let engine_err = |m: String| vec![Violation { focus: String::new(), path: String::new(), message: m, severity: "Engine".into() }];
    let mut data = st.graph.clone();
    for t in st.vocab.ontology.iter() { data.insert(t); } // like pySHACL's ont_graph: subclass axioms visible to sh:class
    let ds = match shacl_rust::validation::dataset::ValidationDataset::from_graphs(data, st.vocab.shapes.clone()) {
        Ok(d) => d, Err(e) => return engine_err(format!("dataset: {e}")),
    };
    let shapes = match shacl_rust::parse_shapes(ds.shapes_graph()) { Ok(s) => s, Err(e) => return engine_err(format!("shapes: {e}")) };
    let json = shacl_rust::validate(&ds, &shapes).as_json();
    let strip = |s: &str| s.trim_matches(|c| c == '<' || c == '>').to_string();
    let mut out = vec![];
    for r in json["results"].as_array().cloned().unwrap_or_default() {
        let sev = r["severity"].as_str().unwrap_or("").to_string();
        if sev.ends_with("Warning") || sev.ends_with("Info") { continue; }
        let msgs = r["messages"].as_array().map(|a| a.iter().filter_map(|m| m.as_str()).collect::<Vec<_>>().join(" | ")).unwrap_or_default();
        out.push(Violation {
            focus: strip(r["focusNode"].as_str().unwrap_or("")),
            path: r["resultPath"].as_str().map(|p| st.vocab.qname(&strip(p))).unwrap_or_default(),
            message: msgs,
            severity: strip(&sev).rsplit('#').next().unwrap_or("").to_string(),
        });
    }
    out
}

fn build_order(v: &Vocab, classes: &[String]) -> Vec<Vec<String>> {
    let wanted: HashSet<String> = classes.iter().filter(|c| v.classes.contains_key(*c)).cloned().collect();
    let deps: HashMap<String, HashSet<String>> = wanted.iter().map(|c| (c.clone(), v.dependencies(c).intersection(&wanted).cloned().collect())).collect();
    let mut placed: HashSet<String> = HashSet::new();
    let mut levels = vec![];
    while placed.len() < wanted.len() {
        let mut ready: Vec<String> = wanted.iter().filter(|c| !placed.contains(*c) && deps[*c].is_subset(&placed)).cloned().collect();
        if ready.is_empty() { ready = wanted.difference(&placed).cloned().collect(); } // cycle: dump the rest in one level
        ready.sort();
        placed.extend(ready.iter().cloned());
        levels.push(ready);
    }
    levels
}

fn short_triple(v: &Vocab, t: TripleRef) -> String {
    let s = match t.subject { NamedOrBlankNodeRef::NamedNode(n) => v.qname(n.as_str()), other => other.to_string() };
    let o = match t.object { TermRef::NamedNode(n) => v.qname(n.as_str()), other => other.to_string() };
    format!("{} {} {}", s, v.qname(t.predicate.as_str()), o)
}

#[tokio::main]
async fn main() -> Result<()> {
    let args: Vec<String> = std::env::args().collect();
    let get = |flag: &str| args.iter().position(|a| a == flag).and_then(|i| args.get(i + 1)).cloned();
    let onto = get("--ontology").ok_or_else(|| anyhow!("usage: kgdc-mcp --ontology X --shapes Y [--ns NS] [--max-subjects N]"))?;
    let shapes = get("--shapes").ok_or_else(|| anyhow!("--shapes required"))?;
    let ns = get("--ns").unwrap_or_else(|| "http://example.org/data/".into());
    let cap = get("--max-subjects").and_then(|s| s.parse().ok()).unwrap_or(40);
    let vocab = Vocab::load(&onto, &shapes, ns)?;
    eprintln!("kgdc-mcp: {} classes, {} properties, {} shape triples", vocab.classes.len(), vocab.properties.len(), vocab.shapes.len());
    let server = Server(Arc::new(Mutex::new(State { vocab, graph: Graph::new(), tasks: vec![], issues: vec![], max_subjects_per_task: cap })));
    let service = server.serve(rmcp::transport::io::stdio()).await?;
    service.waiting().await?;
    Ok(())
}
