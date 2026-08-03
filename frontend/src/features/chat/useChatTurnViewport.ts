import { useCallback, useEffect, useLayoutEffect, useRef } from "react";
import {
  collapseTurnReservation,
  latestTurnTopInset,
  minimumConversationLeadSpacerHeight,
  reanchoredTurnMaxScrollY,
  stableTurnScrollY,
  turnSpacerHeightForMaxScroll,
  turnSpacerHeightWithinCollapseLimit,
  wheelTurnCollapseDistance,
  type CollapsibleTurnReservation,
} from "./chatTurnViewport";

type ChatTurnLayoutMessage = { role: "user" | "assistant" };

type ChatTurnViewportOptions = {
  active: boolean;
  busy: boolean;
  composerDocked: boolean;
  messages: readonly ChatTurnLayoutMessage[];
  operationRefreshSignal: number;
  toolStatus: string | null;
};

type PositionedTurnReservation = CollapsibleTurnReservation & {
  spacerHeightLimit: number | null;
  topInset: number;
  userDocumentTop: number;
};

export function useChatTurnViewport({
  active,
  busy,
  composerDocked,
  messages,
  operationRefreshSignal,
  toolStatus,
}: ChatTurnViewportOptions) {
  const messagesRef = useRef<HTMLDivElement | null>(null);
  const latestUserMessageRef = useRef<HTMLDivElement | null>(null);
  const conversationLeadSpacerRef = useRef<HTMLDivElement | null>(null);
  const turnSpacerRef = useRef<HTMLDivElement | null>(null);
  const composerRef = useRef<HTMLFormElement | null>(null);
  const composerReservationRef = useRef<HTMLDivElement | null>(null);
  const pendingTurnPositionRef = useRef(false);
  const turnReservationRef = useRef<PositionedTurnReservation | null>(null);
  const previousWindowScrollYRef = useRef(0);
  const programmaticTurnScrollRef = useRef(false);
  const turnScrollReleaseFrameRef = useRef<number | null>(null);

  const documentMaxScrollY = useCallback(() => Math.max(
    0,
    document.documentElement.scrollHeight - window.innerHeight,
  ), []);

  const syncComposerDock = useCallback(() => {
    const composer = composerRef.current;
    const reservation = composerReservationRef.current;
    if (!active || !composerDocked || composer === null || reservation === null) return;
    const reservationRect = reservation.getBoundingClientRect();
    const left = `${Math.round(reservationRect.left)}px`;
    const width = `${Math.round(reservationRect.width)}px`;
    if (composer.style.left !== left) composer.style.left = left;
    if (composer.style.width !== width) composer.style.width = width;
    const height = `${Math.ceil(composer.getBoundingClientRect().height)}px`;
    if (reservation.style.height !== height) reservation.style.height = height;
  }, [active, composerDocked]);

  const syncTurnSpacer = useCallback(() => {
    syncComposerDock();
    const spacer = turnSpacerRef.current;
    const reservation = turnReservationRef.current;
    const userMessage = latestUserMessageRef.current;
    if (!active || spacer === null || reservation === null || userMessage === null) return;
    const topInset = latestTurnTopInset(window.innerWidth);
    const userDocumentTop = window.scrollY + userMessage.getBoundingClientRect().top;
    const maxScrollY = reanchoredTurnMaxScrollY(
      reservation.maxScrollY,
      reservation.userDocumentTop,
      reservation.topInset,
      userDocumentTop,
      topInset,
    );
    const probeHeight = Math.max(window.innerHeight, spacer.getBoundingClientRect().height);
    spacer.style.height = `${probeHeight}px`;
    const spacerHeight = turnSpacerHeightWithinCollapseLimit(
      turnSpacerHeightForMaxScroll(
        probeHeight,
        maxScrollY,
        documentMaxScrollY(),
      ),
      reservation.spacerHeightLimit,
    );
    spacer.style.height = `${spacerHeight}px`;
    turnReservationRef.current = {
      maxScrollY,
      spacerHeight,
      spacerHeightLimit: reservation.spacerHeightLimit,
      topInset,
      userDocumentTop,
    };
    const correctedScrollY = Math.min(maxScrollY, documentMaxScrollY());
    if (window.scrollY < correctedScrollY) {
      window.scrollTo({ top: correctedScrollY, behavior: "auto" });
      previousWindowScrollYRef.current = window.scrollY;
    }
  }, [active, documentMaxScrollY, syncComposerDock]);

  const resetTurnViewport = useCallback((resetConversationLead = false) => {
    pendingTurnPositionRef.current = false;
    turnReservationRef.current = null;
    programmaticTurnScrollRef.current = false;
    if (turnScrollReleaseFrameRef.current !== null) {
      cancelAnimationFrame(turnScrollReleaseFrameRef.current);
      turnScrollReleaseFrameRef.current = null;
    }
    if (turnSpacerRef.current !== null) turnSpacerRef.current.style.height = "0px";
    if (resetConversationLead && conversationLeadSpacerRef.current !== null) {
      conversationLeadSpacerRef.current.style.height = "0px";
    }
  }, []);

  const positionLatestTurn = useCallback(() => {
    pendingTurnPositionRef.current = true;
  }, []);

  useLayoutEffect(syncComposerDock, [syncComposerDock]);

  useLayoutEffect(() => {
    if (!active || !pendingTurnPositionRef.current) return;
    const userMessage = latestUserMessageRef.current;
    const leadSpacer = conversationLeadSpacerRef.current;
    const spacer = turnSpacerRef.current;
    if (userMessage === null || leadSpacer === null || spacer === null) return;

    pendingTurnPositionRef.current = false;
    const topInset = latestTurnTopInset(window.innerWidth);
    if (messages.length === 2) {
      leadSpacer.style.height = "0px";
      const leadHeight = minimumConversationLeadSpacerHeight(
        userMessage.getBoundingClientRect().top,
        topInset,
      );
      leadSpacer.style.height = `${leadHeight}px`;
    }
    spacer.style.height = "0px";
    const userDocumentTop = window.scrollY + userMessage.getBoundingClientRect().top;
    const maxScrollY = stableTurnScrollY(userDocumentTop - topInset);
    spacer.style.height = `${window.innerHeight}px`;
    const spacerHeight = turnSpacerHeightForMaxScroll(
      window.innerHeight,
      maxScrollY,
      documentMaxScrollY(),
    );
    spacer.style.height = `${spacerHeight}px`;
    turnReservationRef.current = {
      maxScrollY,
      spacerHeight,
      spacerHeightLimit: null,
      topInset,
      userDocumentTop,
    };
    programmaticTurnScrollRef.current = true;
    window.scrollTo({ top: Math.min(maxScrollY, documentMaxScrollY()), behavior: "auto" });
    previousWindowScrollYRef.current = window.scrollY;
    if (turnScrollReleaseFrameRef.current !== null) {
      cancelAnimationFrame(turnScrollReleaseFrameRef.current);
    }
    turnScrollReleaseFrameRef.current = requestAnimationFrame(() => {
      syncTurnSpacer();
      const settledReservation = turnReservationRef.current;
      if (settledReservation !== null) {
        window.scrollTo({
          top: Math.min(settledReservation.maxScrollY, documentMaxScrollY()),
          behavior: "auto",
        });
      }
      previousWindowScrollYRef.current = window.scrollY;
      turnScrollReleaseFrameRef.current = requestAnimationFrame(() => {
        previousWindowScrollYRef.current = window.scrollY;
        programmaticTurnScrollRef.current = false;
        turnScrollReleaseFrameRef.current = null;
      });
    });
  }, [active, documentMaxScrollY, messages.length, syncTurnSpacer]);

  useLayoutEffect(() => {
    if (!pendingTurnPositionRef.current) syncTurnSpacer();
  }, [busy, messages, operationRefreshSignal, syncTurnSpacer, toolStatus]);

  useEffect(() => {
    if (!active || composerRef.current === null) return;
    const observer = new ResizeObserver(syncTurnSpacer);
    if (messagesRef.current !== null) observer.observe(messagesRef.current);
    observer.observe(composerRef.current);
    const main = composerRef.current.closest("main");
    if (main !== null) observer.observe(main);
    const mainContainer = main?.parentElement;
    if (mainContainer !== null && mainContainer !== undefined) observer.observe(mainContainer);
    window.addEventListener("resize", syncTurnSpacer, { passive: true });
    syncTurnSpacer();
    return () => {
      observer.disconnect();
      window.removeEventListener("resize", syncTurnSpacer);
    };
  }, [active, composerDocked, messages.length > 0, syncTurnSpacer]);

  useEffect(() => {
    if (!active) return;
    previousWindowScrollYRef.current = window.scrollY;
    let previousTouchY: number | null = null;
    let touchScrollTarget: HTMLTextAreaElement | null = null;
    const collapseTurnSpaceBy = (amount: number) => {
      const reservation = turnReservationRef.current;
      const spacer = turnSpacerRef.current;
      if (reservation === null || spacer === null
          || reservation.spacerHeight === 0 || amount <= 0) return false;
      const next = collapseTurnReservation(reservation, amount, 0);
      spacer.style.height = `${next.spacerHeight}px`;
      const positionedNext = {
        ...reservation,
        ...next,
        spacerHeightLimit: Math.min(
          reservation.spacerHeightLimit ?? Number.POSITIVE_INFINITY,
          next.spacerHeight,
        ),
      };
      turnReservationRef.current = positionedNext;
      const correctedScrollY = Math.min(positionedNext.maxScrollY, documentMaxScrollY());
      window.scrollTo({ top: correctedScrollY, behavior: "auto" });
      previousWindowScrollYRef.current = window.scrollY;
      return true;
    };
    const noteWheelIntent = (event: WheelEvent) => {
      if (event.target instanceof HTMLTextAreaElement
          && event.target.scrollTop > 0) return;
      const collapseDistance = wheelTurnCollapseDistance(
        event.deltaY,
        event.deltaMode,
        window.innerHeight,
      );
      if (collapseTurnSpaceBy(collapseDistance)) {
        event.preventDefault();
      }
    };
    const noteTouchStart = (event: TouchEvent) => {
      touchScrollTarget = event.target instanceof HTMLTextAreaElement ? event.target : null;
      previousTouchY = event.touches[0]?.clientY ?? null;
    };
    const noteTouchIntent = (event: TouchEvent) => {
      const currentTouchY = event.touches[0]?.clientY ?? null;
      if (currentTouchY !== null && previousTouchY !== null
          && currentTouchY > previousTouchY && (touchScrollTarget?.scrollTop ?? 0) > 0) {
        previousTouchY = currentTouchY;
        return;
      }
      if (currentTouchY !== null && previousTouchY !== null
          && currentTouchY > previousTouchY
          && collapseTurnSpaceBy(currentTouchY - previousTouchY)) {
        event.preventDefault();
      }
      previousTouchY = currentTouchY;
    };
    const noteTouchEnd = () => {
      previousTouchY = null;
      touchScrollTarget = null;
    };
    const noteKeyboardIntent = (event: KeyboardEvent) => {
      const target = event.target;
      if (target instanceof HTMLElement
          && (target.isContentEditable || ["INPUT", "SELECT", "TEXTAREA"].includes(target.tagName))) {
        return;
      }
      const collapseAmount = event.key === "ArrowUp"
        ? 40
        : event.key === "PageUp"
          ? window.innerHeight * 0.8
          : event.key === "Home"
            ? turnReservationRef.current?.spacerHeight ?? 0
            : 0;
      if (collapseTurnSpaceBy(collapseAmount)) {
        event.preventDefault();
      }
    };
    const collapseUnusedTurnSpace = () => {
      const currentScrollY = window.scrollY;
      if (programmaticTurnScrollRef.current) {
        previousWindowScrollYRef.current = currentScrollY;
        return;
      }
      const reservation = turnReservationRef.current;
      const spacer = turnSpacerRef.current;
      if (reservation !== null && spacer !== null) {
        const movingContentDown = currentScrollY < previousWindowScrollYRef.current;
        if (movingContentDown && reservation.spacerHeight > 0) {
          const next = collapseTurnReservation(
            reservation,
            previousWindowScrollYRef.current,
            currentScrollY,
          );
          if (next.spacerHeight !== reservation.spacerHeight) {
            spacer.style.height = `${next.spacerHeight}px`;
            const positionedNext = {
              ...reservation,
              ...next,
              spacerHeightLimit: Math.min(
                reservation.spacerHeightLimit ?? Number.POSITIVE_INFINITY,
                next.spacerHeight,
              ),
            };
            turnReservationRef.current = positionedNext;
            const correctedScrollY = Math.min(positionedNext.maxScrollY, documentMaxScrollY());
            if (currentScrollY < correctedScrollY) {
              window.scrollTo({ top: correctedScrollY, behavior: "auto" });
              previousWindowScrollYRef.current = window.scrollY;
              return;
            }
          }
        }
      }
      previousWindowScrollYRef.current = currentScrollY;
    };
    window.addEventListener("wheel", noteWheelIntent, { passive: false });
    window.addEventListener("touchstart", noteTouchStart, { passive: true });
    window.addEventListener("touchmove", noteTouchIntent, { passive: false });
    window.addEventListener("touchend", noteTouchEnd, { passive: true });
    window.addEventListener("touchcancel", noteTouchEnd, { passive: true });
    window.addEventListener("keydown", noteKeyboardIntent);
    window.addEventListener("scroll", collapseUnusedTurnSpace, { passive: true });
    return () => {
      window.removeEventListener("wheel", noteWheelIntent);
      window.removeEventListener("touchstart", noteTouchStart);
      window.removeEventListener("touchmove", noteTouchIntent);
      window.removeEventListener("touchend", noteTouchEnd);
      window.removeEventListener("touchcancel", noteTouchEnd);
      window.removeEventListener("keydown", noteKeyboardIntent);
      window.removeEventListener("scroll", collapseUnusedTurnSpace);
      if (turnScrollReleaseFrameRef.current !== null) {
        cancelAnimationFrame(turnScrollReleaseFrameRef.current);
        turnScrollReleaseFrameRef.current = null;
      }
      programmaticTurnScrollRef.current = false;
    };
  }, [active, documentMaxScrollY]);

  return {
    composerRef,
    composerReservationRef,
    conversationLeadSpacerRef,
    latestUserMessageRef,
    messagesRef,
    positionLatestTurn,
    resetTurnViewport,
    turnSpacerRef,
  };
}
