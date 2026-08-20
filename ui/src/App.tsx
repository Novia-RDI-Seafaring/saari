import { Navigate, Route, Routes } from "react-router-dom";
import { Layout } from "./components/Layout";
import { Home } from "./views/Home";
import { Searches } from "./views/Searches";
import { Corpus } from "./views/Corpus";
import { Papers } from "./views/Papers";
import { Review } from "./views/Review";

export default function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<Navigate to="/home" replace />} />
        <Route path="home" element={<Home />} />
        <Route path="searches" element={<Searches />} />
        <Route path="corpus" element={<Corpus />} />
        <Route path="graph" element={<Navigate to="/corpus" replace />} />
        <Route path="papers" element={<Papers />} />
        <Route path="review" element={<Review />} />
      </Route>
    </Routes>
  );
}
