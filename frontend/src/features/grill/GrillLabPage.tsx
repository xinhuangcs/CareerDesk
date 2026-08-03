import { useEffect, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { QuestionsPage } from "../questions/QuestionsPage";
import { GrillExperimentIntro } from "./GrillExperimentIntro";
import { claimGrillExperimentIntro } from "./grillApi";
import { GrillPage } from "./GrillPage";
import { useLocalizer } from "../../i18n/useLocalizer";
import {
  grillExperimentIntroWasSeen,
  markGrillExperimentIntroSeen,
} from "./grillVisibilityPreference";

type LabView = "practice" | "questions";

export function GrillLabPage() {
  const l = useLocalizer();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const activeView: LabView = searchParams.get("view") === "questions" ? "questions" : "practice";
  const [questionsMounted, setQuestionsMounted] = useState(activeView === "questions");
  const [showExperimentIntro, setShowExperimentIntro] = useState(false);
  const [experimentIntroResolved, setExperimentIntroResolved] = useState(false);

  useEffect(() => {
    let active = true;
    void claimGrillExperimentIntro().then(({ should_show, release_version }) => {
      const previouslySeen = grillExperimentIntroWasSeen(release_version);
      if (!previouslySeen) markGrillExperimentIntroSeen(release_version);
      if (active) {
        setShowExperimentIntro(should_show);
        setExperimentIntroResolved(true);
      }
    }).catch(() => {
      if (active) {
        setShowExperimentIntro(true);
        setExperimentIntroResolved(true);
      }
    });
    return () => { active = false; };
  }, []);

  useEffect(() => {
    if (activeView === "questions") setQuestionsMounted(true);
  }, [activeView]);

  function selectView(view: LabView) {
    const next = new URLSearchParams(searchParams);
    if (view === "questions") {
      setQuestionsMounted(true);
      next.set("view", "questions");
    } else {
      next.delete("view");
    }
    setSearchParams(next);
  }

  function closeExperimentIntro() {
    setShowExperimentIntro(false);
  }

  function openExperimentSettings() {
    closeExperimentIntro();
    void navigate({
      pathname: "/settings",
      search: "?section=experiments",
    });
  }

  if (!experimentIntroResolved) return null;

  return (
    <div className="flex min-w-0 flex-col">
      {showExperimentIntro && (
        <GrillExperimentIntro
          onContinue={closeExperimentIntro}
          onOpenSettings={openExperimentSettings}
        />
      )}
      <div className="segmented mb-5 w-fit" role="tablist" aria-label={l("拷打室内容", "Interview Lab content")}>
        <button
          type="button"
          role="tab"
          aria-selected={activeView === "practice"}
          aria-controls="grill-practice-panel"
          onClick={() => selectView("practice")}
          className={`segmented-item ${activeView === "practice" ? "segmented-on" : ""}`}
        >
          {l("开始练习", "Practice")}
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={activeView === "questions"}
          aria-controls="grill-questions-panel"
          onClick={() => selectView("questions")}
          className={`segmented-item ${activeView === "questions" ? "segmented-on" : ""}`}
        >
          {l("题库", "Question Bank")}
        </button>
      </div>

      <section
        id="grill-practice-panel"
        role="tabpanel"
        aria-label={l("开始练习", "Practice")}
        hidden={activeView !== "practice"}
        className="w-full max-w-3xl"
      >
        <GrillPage />
      </section>

      <section
        id="grill-questions-panel"
        role="tabpanel"
        aria-label={l("题库", "Question Bank")}
        hidden={activeView !== "questions"}
        className="w-full"
      >
        {questionsMounted && <QuestionsPage />}
      </section>
    </div>
  );
}
